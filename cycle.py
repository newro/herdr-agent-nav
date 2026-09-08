#!/usr/bin/env python3
"""Move to the previous, next, or last-visited agent.

Why not the built-in previous_agent / next_agent
    They pivot on the *currently focused pane*. Step off onto a shell and there
    is no pivot left, so they jump to the first or last entry in the list. That
    is why glancing at a shell and coming back used to land you somewhere
    unrelated.

What this does instead
    When focus is not on an agent, the pivot is the last-focused agent recorded
    by daemon.py — the row the sidebar marks. So pressing prev/next from a shell
    lands you beside the agent you were just in.

    Ordering follows agent.list, which is both the sidebar order and the
    focus_agent (prefix+1..9) index, so movement never disagrees with the
    numbers on screen.

back
    From a shell, returns to the last agent you were in. From an agent, toggles
    to the one before it, so repeated presses bounce between two agents. herdr
    has last_pane but no last_agent, hence the daemon's record.

Usage:
    cycle.py prev
    cycle.py next
    cycle.py back
    cycle.py next --dry-run   decide but do not move; print the reasoning
"""

import sys

import herdr_api as api


def record_move(target, panes):
    """Write the pivot record ourselves instead of waiting for the daemon.

    The daemon learns about a focus change by polling, so up to half a second
    passes before the record catches up. Press the key twice inside that window
    — which is ordinary use for a toggle, not abuse — and the second press reads
    a record that has not moved yet and goes straight back to where it already
    is. The toggle looks stuck, and prev/next repeats a step.

    Writing here makes the record right the instant the move is requested. The
    daemon reaches the same conclusion when it next polls, so the two cannot
    disagree; this only removes the lag.
    """
    if target not in panes:
        return
    current = api.read_state(api.CURRENT_AGENT_FILE)
    if current and current != target:
        api.write_atomic(api.PREVIOUS_AGENT_FILE, current)
    api.write_atomic(api.CURRENT_AGENT_FILE, target)


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("prev", "next", "back"):
        print("usage: cycle.py <prev|next|back> [--dry-run]", file=sys.stderr)
        return 2
    mode = sys.argv[1]
    step = -1 if mode == "prev" else 1
    dry_run = "--dry-run" in sys.argv[2:]

    try:
        agents = api.result("agent.list", {}).get("agents", [])
        panes = [a.get("pane_id") for a in agents if a.get("pane_id")]
        if not panes:
            return 0
        focused = (
            api.result("session.snapshot", {})
            .get("snapshot", {})
            .get("focused_pane_id")
            or ""
        )
    except (OSError, ValueError, api.HerdrError):
        return 1

    basis = ""
    if mode == "back":
        if focused in panes:
            target = api.read_state(api.PREVIOUS_AGENT_FILE)
            basis = f"on an agent, so the one before it: {target or '(no record)'}"
        else:
            target = api.read_state(api.CURRENT_AGENT_FILE)
            basis = f"on a shell, so the last agent visited: {target or '(no record)'}"
        if target not in panes:
            if dry_run:
                print(f"pivot : {basis}")
                print(f"list  : {panes}")
                print("target: none — doing nothing")
            return 0
    else:
        if focused in panes:
            # Focus is on an agent, so this matches the built-in behaviour.
            base = panes.index(focused)
            basis = f"focused agent {focused}"
        else:
            last = api.read_state(api.CURRENT_AGENT_FILE)
            if last in panes:
                # Move to the neighbour rather than back to the pivot itself.
                # Returning to the pivot is what `back` is for.
                base = panes.index(last)
                basis = f"last agent visited {last}"
            else:
                # No record, or that agent has closed. Fall back to the built-in
                # behaviour and start from the end: index 0 for next, last for
                # prev, which the arithmetic below works out.
                base = -1 if step == 1 else 0
                basis = "no pivot — starting from the end of the list"
        target = panes[(base + step) % len(panes)]

    if dry_run:
        print(f"pivot : {basis}")
        print(f"list  : {panes}")
        print(f"target: {target}")
        return 0

    # pane.focus, not agent.focus.
    #
    # agent.focus does not take a pane id. Given one it answers success anyway
    # and moves focus to some unrelated pane that is not even in the agent list,
    # which looks exactly like "the key does nothing, except once in a while it
    # jumps somewhere random". Passing terminal_id instead is rejected outright
    # with agent_not_found.
    #
    # We already hold pane ids from agent.list, and pane.focus follows the pane
    # across workspaces, so it is both the correct and the simpler call.
    try:
        api.request("pane.focus", {"pane_id": target})
    except (OSError, ValueError, api.HerdrError):
        return 1
    record_move(target, panes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
