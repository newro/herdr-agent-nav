#!/usr/bin/env python3
"""Stamps index numbers onto herdr's sidebar rows, and tracks the last-focused agent.

herdr's built-in sidebar tokens carry no index. Values reported as workspace or
pane metadata, however, render as $name token rows (see the config snippet in
README.md), so this daemon keeps a `num` token current on every row.

  - spaces: the workspace's own number (its position in the list). Labels listed
    in space-pins.conf get the fixed number written there instead, because
    herdr's numbering is positional and cannot be pinned.
  - agents: the 1-based index into `agent.list`. With agent_panel_sort at its
    default of "spaces" that order is both the sidebar order and the index that
    focus_agent (prefix+1..9) uses, so the number you see is the number you type.

Those numbers change whenever a workspace or pane is created, closed, or
reordered, so the daemon subscribes to exactly those events and restamps only
when one arrives. There is no polling.

It also records which agent was last focused. That record is what puts the
marker on the sidebar row, and what cycle.py pivots on.

Usage:
  daemon.py --restore    start the daemon unless one is already running
  daemon.py --daemon     run in the foreground (what --restore spawns)
  daemon.py --resolve N  print the workspace id pinned to number N
"""

import fcntl
import glob
import json
import os
import select
import socket
import subprocess
import sys
import time
from fnmatch import fnmatchcase

import herdr_api as api

TOKEN = "num"  # pairs with $num in the sidebar row config

# Marker for the last-focused agent.
#
# A background colour is not available. herdr's sidebar token style carries only
# token/fg/bold/dim, and a `bg` key makes the whole config fail to parse. The
# metadata API's `tokens` is a {name: string|null} map, so no colour can travel
# with the value either — colour has to be fixed per token name in the config.
#
# So instead of a background this stamps a block glyph on that one row and lets
# the config colour it. The glyph itself is the marker, so it reads even when
# the colour does not carry.
TOKEN_BAR = "lastbar"  # pairs with $lastbar
MARK_LAST = "▐█"

# Never rendered. It exists to force a sidebar redraw.
#
# herdr treats workspace.report_metadata as changing the UI and redraws the
# sidebar for it; pane.report_metadata does not get that treatment (it does not
# even reach the server log). So a marker that only moves between agent rows
# updates the token and then sits unseen until something else forces a redraw —
# a keypress, or in practice another plugin's periodic workspace update, which
# is why the marker looked like it lagged by several seconds or needed an Enter.
#
# Carrying the marker's location as workspace metadata too means every marker
# move is part of a workspace update, so the redraw comes with it. Only the
# workspace that gains the marker and the one that loses it change value, so
# this costs at most two extra requests per move. Leave it out of the sidebar
# row config and it stays invisible.
TOKEN_MARKER_AT = "markerat"

RECONNECT_DELAY = 2.0  # seconds between reconnect attempts
RECONNECT_MAX = 30  # give up after roughly a minute; the next event restarts us

# How often to ask who holds focus when no event has arrived. See poll_focus for
# why polling is unavoidable. Half a second keeps the marker feeling immediate
# while costing one small request per interval, and only when the daemon would
# otherwise be sitting idle.
FOCUS_POLL_SEC = 0.5

# Restamp flood guard: more than this many restamps of one workspace inside the
# window and we stop fighting over it. As observed, herdr's token merge lets the
# last writer win, so one restamp settles it — this only matters if something
# starts hitting back, and then it keeps us off the CPU.
REASSERT_MAX = 5
REASSERT_WINDOW = 10.0

# Only events that can change a number.
#
#  - pane.updated must NOT be subscribed: the metadata we stamp raises it again,
#    which is an infinite loop.
#  - workspace.metadata_updated is subscribed on purpose, to notice when someone
#    overwrites what we wrote (see `clobbered`). It only reacts when the observed
#    value differs from what we sent, so the echo of our own stamp does nothing
#    and no loop forms.
#  - pane.agent_status_changed fires continuously while an agent works and never
#    affects ordering, so it is left out.
SUBSCRIPTIONS = [
    {"type": "workspace.created"},
    {"type": "workspace.closed"},
    {"type": "workspace.moved"},
    {"type": "workspace.reordered"},
    {"type": "workspace.renamed"},  # pins match on label, so renames matter
    {"type": "workspace.metadata_updated"},  # to notice someone clobbering us
    {"type": "pane.created"},
    {"type": "pane.closed"},
    {"type": "pane.exited"},
    {"type": "pane.moved"},
    {"type": "pane.agent_detected"},  # a pane joins the agent list
    {"type": "pane.focused"},  # unrelated to numbers — this is the focus record
]

PINS_FILE = "space-pins.conf"
LOCK = api.state_path("agent-nav.lock")
MIRROR_STATE = os.path.expanduser("~/.local/state/herdr-mirror")


def load_pins():
    """[(label pattern, "number")] — an absent or broken file just means no pins.

    Patterns support globs (*, ?) and otherwise match exactly. File order is
    preserved so that when several patterns match, the first one wins.
    """
    pins = []
    try:
        with open(api.config_path(PINS_FILE), encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                num, label = line.split("=", 1)
                num, label = num.strip(), label.strip()
                if num and label:
                    pins.append((label, num))
    except OSError:
        pass
    return pins


def mirror_local_ids(host=None):
    """Local workspace ids that herdr-mirror is currently mirroring.

    The mirror daemon writes a remote->local map to <host>-map.json. A mirrored
    workspace follows the remote's name ("laptop: api" becomes "laptop: web"),
    so pinning it by label would come undone; this map is the stable handle.

    Absent herdr-mirror the directory does not exist and this yields nothing,
    which is exactly right — @mirror pins simply never match.
    """
    ids = set()
    pattern = f"{host}-map.json" if host else "*-map.json"
    for path in sorted(glob.glob(os.path.join(MIRROR_STATE, pattern))):
        try:
            with open(path, encoding="utf-8") as fh:
                m = json.load(fh)
        except (OSError, ValueError):
            continue
        for entry in m.get("workspaces", {}).values():
            local = entry.get("localId")
            if local:
                ids.add(local)
    return ids


def pinned_num(ws, pins, mirror_cache):
    """The fixed number for a workspace, or None.

    Pattern forms:
      @mirror:<host>  a workspace herdr-mirror mirrors from that host
      @mirror         any mirrored workspace
      anything else   a label glob (*, ?), or an exact match without them
    """
    for pattern, num in pins:
        if pattern == "@mirror" or pattern.startswith("@mirror:"):
            host = pattern.split(":", 1)[1].strip() if ":" in pattern else None
            if host not in mirror_cache:
                mirror_cache[host] = mirror_local_ids(host)
            if ws.get("workspace_id") in mirror_cache[host]:
                return num
        elif fnmatchcase(ws.get("label", ""), pattern):
            return num
    return None


def apply_tokens(method, key_name, targets, cache):
    """targets: [(id, {token: value})]. Unchanged values are not resent."""
    seen = set()
    for ident, tokens in targets:
        seen.add(ident)
        if cache.get(ident) == tokens:
            continue
        api.request(method, {key_name: ident, "source": api.SOURCE, "tokens": tokens})
        cache[ident] = tokens
    for gone in set(cache) - seen:  # drop what closed
        cache.pop(gone, None)


def list_workspaces():
    return api.result("workspace.list", {}).get("workspaces", [])


def list_agents():
    return api.result("agent.list", {}).get("agents", [])


def stamp_spaces(cache, agents, pinned, marker_ws=None):
    """spaces row: $num plus $panes / $agents (how many are open).

    herdr's built-in space tokens carry no counts. Pane counts come from
    workspace.list; agent counts are agent.list grouped by workspace.

    `pinned` collects the workspaces a fixed number was applied to, and restamping
    is limited to those. An unpinned number is just herdr's own list position, so
    there is nothing to win back if something overwrites it — and fighting over a
    mirrored workspace's counts would mean trading writes with the mirror plugin
    forever.
    """
    pins = load_pins()
    mirror_cache = {}  # read each map file at most once per stamp
    agent_count = {}
    for a in agents:
        wid = a.get("workspace_id")
        if wid:
            agent_count[wid] = agent_count.get(wid, 0) + 1
    targets = []
    pinned.clear()
    for w in list_workspaces():
        wid = w.get("workspace_id")
        if not wid:
            continue
        pin = pinned_num(w, pins, mirror_cache)
        if pin:
            pinned.add(wid)
        targets.append(
            (
                wid,
                {
                    TOKEN: pin or str(w.get("number", "")),
                    "panes": f"panel({w.get('pane_count', 0)})",
                    "agents": f"agent({agent_count.get(wid, 0)})",
                    # Invisible; see TOKEN_MARKER_AT. This is what makes a marker
                    # move ride along with a workspace update, so the sidebar
                    # redraws immediately instead of at the next unrelated one.
                    TOKEN_MARKER_AT: wid if wid == marker_ws else None,
                },
            )
        )
    apply_tokens("workspace.report_metadata", "workspace_id", targets, cache)


def stamp_agents(cache, agent_panes, agents, last_focused=None):
    """agents row: $num, plus $lastbar on the one row that carries the marker.

    agent.list order is the sidebar order and the focus_agent index, so the
    number shown is the number to type. Rows that should not carry the marker
    must be sent None explicitly, or a stale glyph stays behind.
    """
    targets = [
        (
            a["pane_id"],
            {
                TOKEN: str(i),
                TOKEN_BAR: MARK_LAST if a["pane_id"] == last_focused else None,
            },
        )
        for i, a in enumerate(agents, 1)
        if a.get("pane_id")
    ]
    # Keep the agent pane set current so a focus event can tell "is this an
    # agent?" without a request. Every event that changes the agent set
    # (created / closed / exited / agent_detected) passes through here.
    agent_panes.clear()
    agent_panes.update(pane for pane, _ in targets)
    apply_tokens("pane.report_metadata", "pane_id", targets, cache)


def record_agent_focus(pane_id, history, agent_panes):
    """Track movement between agent panes only.

    Stepping out to an ordinary shell and back is ignored, so the toggle target
    does not drift. Focus changes we cause ourselves land here too, which is what
    makes A -> B -> back -> A -> back -> B bounce between two agents.

    Returns whether the "last agent" changed, so the caller knows to restamp and
    move the sidebar marker.
    """
    if pane_id not in agent_panes or pane_id == history.get("current"):
        return False
    if history.get("current"):
        history["previous"] = history["current"]
    history["current"] = pane_id
    # This value outlives focus moving out to a shell. It is the pivot for
    # prev/next.
    api.write_atomic(api.CURRENT_AGENT_FILE, pane_id)
    previous = history.get("previous")
    if previous:
        api.write_atomic(api.PREVIOUS_AGENT_FILE, previous)
    return True


def marker_target(history):
    """Which pane should carry the marker glyph, if any.

    When the last-focused agent is *still* focused, herdr's own selection bar
    already highlights that row, so no marker is added. The marker appears the
    moment focus leaves that row, standing in for the selection bar that just
    went away.
    """
    current = history.get("current")
    if not current or current == history.get("live"):
        return None
    return current


def seed_focus(history, agents):
    """Recover the "last agent" when the daemon starts.

    Without this, the sidebar highlight is blank after a herdr or daemon restart
    until the first agent focus, and prev/next start from the end of the list
    with no pivot. The saved record is tried first; if that agent has since
    closed, the currently focused pane is used when it happens to be an agent.
    """
    if history.get("current") and history.get("live"):
        return
    panes = {a.get("pane_id") for a in agents if a.get("pane_id")}
    # `live` must always be filled in. Left empty, a focused agent would also get
    # the marker and it would sit on top of herdr's selection bar.
    focused = (
        api.result("session.snapshot", {}).get("snapshot", {}).get("focused_pane_id")
    )
    history["live"] = focused
    if history.get("current"):
        return
    # A focused agent beats the saved record. If focus is sitting on an agent
    # right now, that *is* the last agent visited, and trusting the file instead
    # would put the marker on whichever agent happened to be current when the
    # daemon last stopped — visibly wrong the moment the daemon restarts while
    # you are working inside an agent.
    if focused in panes:
        history["current"] = focused
        api.write_atomic(api.CURRENT_AGENT_FILE, focused)
        return
    # Focus is on a shell, so the file is the only thing that knows where you
    # were. Without it a restart would leave prev/next with no pivot.
    saved = api.read_state(api.CURRENT_AGENT_FILE)
    if saved in panes:
        history["current"] = saved


def clobbered(sent, observed):
    """True when any token we last sent now reads back as something else.

    herdr-mirror forwards a remote workspace's tokens onto the local mirror. Its
    source differs from ours, so herdr shows whichever was written last — and
    since the same stamper runs on the remote, that remote's positional $num
    arrives and buries our pinned number. apply_tokens caches what it sent and
    skips resending an identical value, so without this check we would believe
    we had already written the pin and never write it again.

    Keys sent as None (marker removal) are skipped: they were sent to clear a
    value, so there is nothing to observe.
    """
    return any(v is not None and observed.get(k) != v for k, v in sent.items())


def reassert_allowed(wid, guard):
    """Flood guard. False once the allowance inside the window is spent."""
    now = time.monotonic()
    count, first = guard.get(wid, (0, now))
    if now - first > REASSERT_WINDOW:
        count, first = 0, now
    if count >= REASSERT_MAX:
        return False
    guard[wid] = (count + 1, first)
    return True


def forget_closed_agent(history, panes):
    """Drop the pivot once its agent is gone.

    `current` is remembered across everything, but if that agent has since
    closed it can never receive a marker again and prev/next would pivot on a
    pane that is not in the list. Clearing it lets seed_focus pick a live one.
    """
    if history.get("current") and history["current"] not in panes:
        history["current"] = None
        history["previous"] = (
            history.get("previous") if history.get("previous") in panes else None
        )


def stamp(cache, agents=None):
    if agents is None:
        agents = list_agents()  # used for both the counts and the agent numbers
    forget_closed_agent(cache["focus"], {a.get("pane_id") for a in agents})
    seed_focus(cache["focus"], agents)
    marker = marker_target(cache["focus"])
    marker_ws = next(
        (a.get("workspace_id") for a in agents if a.get("pane_id") == marker), None
    )
    # Agents first, spaces second, and the order matters. The spaces stamp is the
    # one that triggers the redraw (see TOKEN_MARKER_AT), so the pane tokens have
    # to already carry the new marker position when it goes out — otherwise the
    # redraw paints the state from before the move.
    stamp_agents(cache["agent"], cache["agent_panes"], agents, marker)
    stamp_spaces(cache["ws"], agents, cache["pinned"], marker_ws)


def handle_focus(cache, pane_id):
    """Apply a focus change, whichever way the news arrived.

    Called from the pane_focused event and from poll_focus alike, so the two
    paths cannot drift apart.
    """
    if not pane_id or pane_id == cache["focus"].get("live"):
        return
    before = marker_target(cache["focus"])
    cache["focus"]["live"] = pane_id
    # Refresh the agent set from the server rather than trusting whatever the
    # last stamp left behind.
    #
    # A pane leaves the agent list the moment its agent exits, and nothing we
    # can subscribe to announces that: pane.exited is the pane itself dying, not
    # the agent inside it, and pane.agent_status_changed requires a pane_id so it
    # cannot be subscribed for "any pane". Left stale, the set still lists a pane
    # that is now an ordinary shell — moving there would look like "moved to an
    # agent", the pivot would advance onto it, and current == live would hide the
    # marker for good.
    agents = None
    try:
        agents = list_agents()
        cache["agent_panes"].clear()
        cache["agent_panes"].update(a["pane_id"] for a in agents if a.get("pane_id"))
    except (OSError, ValueError, api.HerdrError) as exc:
        api.log("agent list refresh failed: %r" % exc)
    record_agent_focus(pane_id, cache["focus"], cache["agent_panes"])
    after = marker_target(cache["focus"])
    if after != before:
        stamp(cache, agents)
    # One line per focus change. This is the only place the daemon's view of
    # "where focus is and where the marker should be" becomes visible from
    # outside, and every marker bug so far looked identical without it: a live
    # process, a healthy socket, and a marker in the wrong place. Written only
    # on an actual change, and the file is truncated at startup.
    api.log(
        "focus %s (agent=%s) marker %s -> %s"
        % (pane_id, pane_id in cache["agent_panes"], before or "-", after or "-")
    )


def poll_focus(cache):
    """Ask the server who holds focus, because the event does not always say.

    Measured against 0.9.0-preview, herdr emits pane_focused only when an
    **agent** pane gains focus. Moving to an ordinary shell pane — with
    ctrl+h/j/k/l, which is the common way to glance away and come back — produces
    no event at all. A daemon driven purely by events therefore never learns that
    focus left the agent, keeps `live` pointing at it, and with current == live
    the marker stays hidden forever. That was the whole "marker never appears"
    bug, and no subscription can fix it.

    (A pane.focus API call does emit the event even for a shell pane, which is
    why the marker looked fine whenever it was exercised through the API and
    broken whenever a human used the keyboard.)

    So focus is polled. One session.snapshot costs a couple of milliseconds and
    nothing else is fetched unless the focused pane actually changed.
    """
    try:
        fp = (
            api.result("session.snapshot", {})
            .get("snapshot", {})
            .get("focused_pane_id")
        )
    except (OSError, ValueError, api.HerdrError):
        return
    handle_focus(cache, fp)


def run_once(cache):
    """Handle events while the subscription lives. Returns when it drops."""
    # Forget the remembered "currently focused pane" on every fresh subscription.
    #
    # `current` and `previous` survive a reconnect on purpose — they are the
    # pivot and the toggle target, and losing them would strand prev/next. But
    # `live` is a mirror of the server's state, and any focus change during the
    # gap arrived while nobody was listening. Keeping a stale `live` makes
    # marker_target compare against a pane that is no longer focused, so the
    # marker silently sits on the wrong row, or nowhere, until the next restart.
    # Dropping it here makes seed_focus read the real value back.
    cache["focus"].pop("live", None)
    s = socket.socket(socket.AF_UNIX)
    s.connect(api.SOCKET_PATH)
    # Bytes are read straight off the socket instead of through makefile().
    # The focus poll below waits on select(), and select() cannot see a complete
    # line that a buffered reader has already pulled in — the daemon would sit
    # there polling while an event waited in the buffer.
    s.sendall(
        (
            json.dumps(
                {
                    "id": api.SOURCE + "-sub",
                    "method": "events.subscribe",
                    "params": {"subscriptions": SUBSCRIPTIONS},
                }
            )
            + "\n"
        ).encode()
    )
    buf = b""
    while b"\n" not in buf:  # subscription ack
        chunk = s.recv(65536)
        if not chunk:
            return
        buf += chunk
    ack, buf = buf.split(b"\n", 1)
    try:
        if json.loads(ack).get("error"):
            # One unsupported subscription type rejects the whole request, and
            # the result is indistinguishable from "this server sends no events".
            # Say which, so the next person does not spend an afternoon on it.
            api.log("subscribe rejected: %s" % ack.decode("utf-8", "replace")[:200])
            return
    except ValueError:
        pass
    try:
        stamp(cache)  # settle the current state right after subscribing
        while True:
            # Drain whatever is already buffered before waiting on the socket,
            # otherwise select() would report "nothing to read" with a complete
            # event still sitting here.
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if line.strip() and not handle_event(cache, line):
                    return
            ready, _, _ = select.select([s], [], [], FOCUS_POLL_SEC)
            if not ready:
                poll_focus(cache)
                continue
            chunk = s.recv(65536)
            if not chunk:  # server stopped or restarted
                return
            buf += chunk
    finally:
        s.close()


def handle_event(cache, line):
    """Dispatch one event line. Returns False when the subscription is lost."""
    try:
        envelope = json.loads(line)
    except ValueError:
        return True
    event = envelope.get("event")
    try:
        if event == "pane_focused":
            # Focus moves do not change numbers, so handle_focus records and
            # restamps only when the marker itself has to move. Panes are
            # focused often and restamping each time would be wasted requests.
            handle_focus(cache, (envelope.get("data") or {}).get("pane_id"))
        elif event == "workspace_metadata_updated":
            # A pinned workspace whose tokens no longer read back as what we
            # sent has been overwritten. Clearing the cache entry is what makes
            # apply_tokens send the same value again. The event carries no
            # source, so "who wrote it" is not available — only "does what is
            # shown still match what we meant". The echo of our own stamp
            # matches and is filtered out here, so no loop forms.
            ws = (envelope.get("data") or {}).get("workspace") or {}
            wid = ws.get("workspace_id")
            sent = cache["ws"].get(wid)
            if (
                wid in cache["pinned"]
                and sent
                and clobbered(sent, ws.get("tokens") or {})
                and reassert_allowed(wid, cache["reassert"])
            ):
                cache["ws"].pop(wid, None)
                stamp(cache)
        else:
            # A normal stamp got through on some other event, so we are not in a
            # restamp fight.
            cache["reassert"].clear()
            stamp(cache)
    except (OSError, ValueError, api.HerdrError) as exc:
        api.log("event %s dropped the subscription: %r" % (event, exc))
        return False
    except Exception as exc:  # noqa: BLE001 - see below
        # Anything unexpected used to escape here and kill the daemon outright,
        # leaving a plugin that looks installed and does nothing. Log it and
        # carry on: a single malformed event is not worth losing the marker and
        # the focus record over.
        api.log("event %s raised, continuing: %r" % (event, exc))
    return True


def resolve(num):
    """Pinned number -> workspace id.

    Goes through exactly the same matching path as the stamp, so the number on
    screen and the workspace it moves to cannot disagree.
    """
    pins = load_pins()
    mirror_cache = {}
    for w in list_workspaces():
        if pinned_num(w, pins, mirror_cache) == num:
            return w.get("workspace_id", "")
    return ""


def acquire_lock():
    """Hold the lock for the process lifetime, or return None if someone has it."""
    lock = open(LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        return None
    return lock


def restore():
    """Start the daemon unless one is already running, then return immediately.

    herdr waits for startup and event commands, so this must not block. The
    probe below is only an optimisation: the spawned child takes the lock for
    real and exits quietly if it lost a race, so a duplicate can never survive.
    """
    probe = acquire_lock()
    if probe is None:
        return 0  # already running
    probe.close()  # releases the lock; the child takes it properly
    subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "--daemon"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        cwd=api.PLUGIN_ROOT,
    )
    return 0


def serve():
    lock = acquire_lock()
    if lock is None:
        return 0  # someone else got there first
    cache = {
        "ws": {},
        "agent": {},
        "agent_panes": set(),
        "focus": {},
        "pinned": set(),  # workspaces carrying a fixed number (restamp targets)
        "reassert": {},  # wid -> (count, window start)
    }
    # Start each run with a fresh log so it cannot grow without bound.
    try:
        open(api.state_path("daemon.log"), "w").close()
    except OSError:
        pass
    api.log("daemon started (pid %d)" % os.getpid())

    failures = 0
    while failures < RECONNECT_MAX:
        try:
            run_once(cache)
            failures = 0  # we were connected and then dropped
            api.log("subscription ended, reconnecting")
        except (OSError, ValueError, api.HerdrError) as exc:
            failures += 1
            api.log("connect failed (%d/%d): %r" % (failures, RECONNECT_MAX, exc))
        except Exception as exc:  # noqa: BLE001
            failures += 1
            api.log("unexpected error (%d/%d): %r" % (failures, RECONNECT_MAX, exc))
        cache["ws"].clear()  # a new server means the metadata is gone too
        cache["agent"].clear()
        cache["pinned"].clear()
        cache["reassert"].clear()
        time.sleep(RECONNECT_DELAY)
    api.log("giving up after %d failures; an event hook will restart us" % failures)
    return 0


def main():
    argv = sys.argv[1:]
    if argv[:1] == ["--resolve"] and len(argv) > 1:
        try:
            print(resolve(argv[1]))
        except (OSError, ValueError, api.HerdrError):
            return 1
        return 0
    if argv[:1] == ["--daemon"]:
        return serve()
    if argv[:1] == ["--restore"] or not argv:
        return restore()
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
