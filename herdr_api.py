"""Shared plumbing: socket calls, plugin directories, atomic writes.

Kept dependency-free on purpose. The plugin ships as plain python3 so there is
no build step and nothing to install.
"""

import json
import os
import socket
import time

PLUGIN_ID = "newro.agent-nav"
SOURCE = "agent-nav"  # metadata source id, also used as the request id

# herdr hands these to every plugin command. The fallbacks matter more than they
# look: these scripts are also called directly — `daemon.py --resolve` from a
# shell script, `cycle.py --dry-run` while developing — and such a process gets
# none of herdr's variables. Falling back to herdr's own standard layout rather
# than to the plugin directory is what keeps a direct call reading the same
# config and state as the daemon. Guessing the plugin directory instead meant a
# direct --resolve read the shipped example and found no pins at all.
SOCKET_PATH = os.environ.get("HERDR_SOCKET_PATH") or os.path.expanduser(
    "~/.config/herdr/herdr.sock"
)
PLUGIN_ROOT = os.environ.get("HERDR_PLUGIN_ROOT") or os.path.dirname(
    os.path.abspath(__file__)
)
STATE_DIR = os.environ.get("HERDR_PLUGIN_STATE_DIR") or os.path.expanduser(
    f"~/.local/state/herdr/plugins/{PLUGIN_ID}"
)
CONFIG_DIR = os.environ.get("HERDR_PLUGIN_CONFIG_DIR") or os.path.expanduser(
    f"~/.config/herdr/plugins/config/{PLUGIN_ID}"
)


class HerdrError(Exception):
    """The server answered, but with an error object."""


def request(method, params):
    """One connection per request — the server answers once and closes.

    Pushing several requests down one connection means the second reply never
    arrives, so every call opens its own socket.
    """
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(5)
    try:
        s.connect(SOCKET_PATH)
        f = s.makefile("rwb")
        f.write(
            (json.dumps({"id": SOURCE, "method": method, "params": params}) + "\n").encode()
        )
        f.flush()
        line = f.readline()
    finally:
        s.close()
    if not line:
        raise ConnectionError("no response")
    msg = json.loads(line)
    if msg.get("error"):
        raise HerdrError(msg["error"].get("message", "unknown error"))
    return msg


def result(method, params):
    return request(method, params).get("result", {})


def state_path(name):
    """State lives outside the plugin dir, so the directory may not exist yet.

    herdr creates it for a plugin-spawned process, but a direct call resolves
    STATE_DIR from the fallback above and finds nothing there.
    """
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
    except OSError:
        pass
    return os.path.join(STATE_DIR, name)


def config_path(name):
    """The user's config if present, otherwise the shipped example."""
    user = os.path.join(CONFIG_DIR, name)
    if os.path.exists(user):
        return user
    beside = os.path.join(PLUGIN_ROOT, name)
    if os.path.exists(beside):
        return beside
    return os.path.join(PLUGIN_ROOT, name + ".example")


def write_atomic(path, text):
    """Write to a temp file and rename over the target.

    A reader must never see a half-written value, and these files are read by a
    separate process on every keypress.
    """
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except OSError:
        pass


def read_state(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def log(message):
    """Append one line to the daemon log.

    The daemon runs detached with its output discarded, so without this there is
    no way to tell afterwards whether it reconnected, hit an exception, or simply
    stopped receiving events — all of which look identical from outside: a live
    process holding a socket and stale state.

    Kept append-only and best-effort; a logging failure must never take the
    daemon down. The file is truncated at startup so it cannot grow forever.
    """
    try:
        with open(state_path("daemon.log"), "a", encoding="utf-8") as fh:
            fh.write("%s  %s\n" % (time.strftime("%F %T"), message))
    except OSError:
        pass


# Written by the daemon, read by the cycle actions.
#
# CURRENT is "the agent you are in, or were last in" — it survives focus moving
# out to a shell, which is what lets prev/next pivot sensibly from a shell.
# PREVIOUS is the one before that, which `back` toggles against.
CURRENT_AGENT_FILE = state_path("current-agent")
PREVIOUS_AGENT_FILE = state_path("previous-agent")
