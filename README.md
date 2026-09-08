# herdr-agent-nav

A [herdr](https://github.com/herdrdev/herdr) plugin that numbers the sidebar and
makes agent switching land where you expect — tmux's `last-window` muscle
memory, for herdr's agents.

Two things, one plugin, because they share a single piece of state: **which
agent were you last looking at.**

**The sidebar has no numbers.** `prefix+1..9` focuses an agent by index and
`alt+1..9` switches to a space by index, but herdr's built-in sidebar rows carry
no index — so there is nothing on screen to aim at and you end up counting rows.

**Switching loses its place.** herdr's built-in `previous_agent` /
`next_agent` pivot on the *currently focused pane*. Step off onto a shell — which
you do constantly — and there is no pivot left, so they jump to the first or last
entry in the list instead of the neighbour of wherever you actually were.

This plugin stamps the numbers, and reuses the same last-focused-agent record to
give switching a pivot that survives stepping away.

## Features

- **Numbers on every sidebar row.** Spaces show their list position, agents show
  their `focus_agent` index — so the number you see is the number you type.
- **Open counts per space.** How many panes and how many agents.
- **A marker on the agent you were last in**, so you can see where `back` will
  take you once focus has moved away.
- **prev / next / back actions** that pivot on that agent rather than on the pane
  you happen to be standing in. Press `next` from a shell and you land beside the
  agent you were just in, not at the end of the list.
- **Pinned space numbers** (optional). herdr numbers spaces positionally, so a
  space's number shifts whenever one before it is created, closed, or reordered.
  Pin one and it keeps its number — including `0`, which herdr's own indexed
  bindings cannot reach.
- **Works across spaces.** Switching to an agent in another space moves you
  there.

## Install

```bash
herdr plugin install newro/herdr-agent-nav
```

For local development, link a checkout instead:

```bash
herdr plugin link /path/to/herdr-agent-nav
```

Requires herdr `0.8.0` or later, and `python3` (3.8+). No third-party packages,
so there is no build step.

### `python3` on the herdr server's `PATH`

This plugin runs `python3`. Herdr's plugin commands run inside the herdr
**server** process, not your interactive shell — if the server was launched from
a GUI terminal, a session manager, or a login item, it commonly inherits a bare
system `PATH` that excludes shims from pyenv, asdf, mise, and the like, even
though `python3` works fine when you type it yourself.

If an action fails with `No such file or directory (os error 2)`, that is this.
Fix it with either:

```bash
sudo ln -s "$(which python3)" /usr/local/bin/python3
```

or quit and relaunch herdr from a shell that already has `python3` on `PATH`.

On macOS the system `/usr/bin/python3` is usually enough, so this rarely bites
there.

## Keybindings

Add to `~/.config/herdr/config.toml`:

```toml
[[keys.command]]
key = "ctrl+comma"
type = "plugin_action"
command = "newro.agent-nav.prev"
description = "previous agent"

[[keys.command]]
key = "ctrl+period"
type = "plugin_action"
command = "newro.agent-nav.next"
description = "next agent"

[[keys.command]]
key = "ctrl+slash"
type = "plugin_action"
command = "newro.agent-nav.back"
description = "back to last agent"
```

And clear the built-ins so they do not compete:

```toml
[keys]
previous_agent = ""
next_agent = ""
```

Any key form herdr accepts works — `prefix+`, `alt+`, plain modifiers. If you
already use `alt+comma` / `alt+period` for tab movement (tmux's `M-,` / `M-.`),
putting agent movement on the `ctrl+` versions keeps the pairing obvious.

| Action | Suggested key | What it does |
|---|---|---|
| `prev` | `ctrl+comma` | Move to the agent before the pivot |
| `next` | `ctrl+period` | Move to the agent after the pivot |
| `back` | `ctrl+slash` | Return to the pivot; press again to toggle between two agents |

The pivot is the agent you were last in — the row the marker points at. From an
agent, `prev`/`next` behave like the built-ins; from a shell they use the marked
agent, which is the whole point.

> **If `ctrl+,` and `ctrl+.` do nothing in your terminal:** those two
> combinations have no legacy encoding and travel only over the Kitty keyboard
> protocol. Where that path is unavailable, have the terminal translate them into
> a herdr prefix sequence instead. In Ghostty, with the default `ctrl+o` prefix:
>
> ```
> keybind = ctrl+comma=text:\x0f,
> keybind = ctrl+period=text:\x0f.
> ```
>
> and bind `prefix+,` / `prefix+.` in herdr. Bind **one** trigger form only —
> registering both the symbol (`ctrl+,`) and the physical key (`ctrl+comma`)
> makes a single press fire twice, which moves two agents at a time.

## Sidebar setup

**Nothing appears until you tell herdr where to put the numbers.** Metadata
values render as `$name` tokens in sidebar rows, so add them to
`~/.config/herdr/config.toml`:

```toml
[ui.sidebar.spaces]
rows = [
  ["state_icon", { token = "$num", fg = "#D3C6AA", dim = false }, "workspace"],
  [{ token = "$panes", fg = "#D3C6AA", dim = false },
   { token = "$agents", fg = "#D3C6AA", dim = false }],
]

[ui.sidebar.agents]
rows = [
  ["state_icon",
   { token = "$num", fg = "#D3C6AA", dim = false },
   { token = "$lastbar", fg = "#8C4652", dim = false, bold = true },
   "workspace"],
  ["terminal_title_stripped"],
]
```

| Token | Row | Shows |
|---|---|---|
| `$num` | spaces | List position, or the pinned number |
| `$num` | agents | The `focus_agent` index |
| `$panes` | spaces | Open pane count |
| `$agents` | spaces | Open agent count |
| `$lastbar` | agents | Marker on the agent you were last in |

Two notes on styling, both learned the hard way:

- **`$`-prefixed metadata tokens render dim by default.** `dim = false` alone is
  not always enough; set `fg` explicitly for rows made entirely of metadata, or
  they read noticeably darker than rows containing built-in tokens.
- **There is no background colour.** A sidebar token style carries only
  `token` / `fg` / `bold` / `dim`, and adding `bg` makes the whole config fail to
  parse. That is why `$lastbar` is a block glyph coloured in the foreground
  rather than a real highlight — herdr draws the selection bar's background
  itself and no other row can imitate it.

The marker appears only once focus **leaves** that row. While you are on it,
herdr's own selection bar already highlights it.

## Pinned space numbers

Optional. Copy the example into the plugin's config directory:

```bash
cp space-pins.conf.example \
   "$(herdr plugin config-dir newro.agent-nav)/space-pins.conf"
```

```
# <number> = <pattern>
0 = @mirror:laptop     # a space herdr-mirror mirrors from that host
7 = scratch             # exact label
8 = notes-*             # label glob
```

`@mirror:<host>` matches by [herdr-mirror](https://github.com/nikok6/herdr-mirror)'s
map rather than by label, because a mirrored space follows the remote's name — a
label pin would come undone the moment the remote switches. Without herdr-mirror
installed the pattern simply never matches.

To jump to a pinned space from a script:

```bash
id=$(python3 daemon.py --resolve 0) && [ -n "$id" ] && herdr workspace focus "$id"
```

## How it works

One daemon subscribes to the events that can change a number
(`workspace.created` / `closed` / `moved` / `reordered` / `renamed`,
`pane.created` / `closed` / `exited` / `moved` / `agent_detected`) and restamps
only when one arrives.

It also tracks which agent was last focused. That record is both the marker's
position and the pivot the three actions use, and it is written to the plugin's
state directory so the actions — separate short-lived processes — can read it.

The daemon holds a `flock`, so every entry point can simply say "start it": a
duplicate exits immediately. A startup hook covers server restarts and handoffs;
a `workspace.focused` hook covers being installed into an already-running herdr,
which does not run startup hooks.

### Why focus is polled

Measured against herdr `0.9.0-preview`, `pane_focused` fires only when an
**agent** pane gains focus. Moving to an ordinary shell pane — with
`ctrl+h/j/k/l`, which is how you glance away and come back — produces no event at
all, and neither does `prefix+<n>`. A daemon driven purely by events therefore
never learns that focus left the agent, keeps its idea of "currently focused"
pointing at it, and the marker stays hidden forever.

(A `pane.focus` API call *does* emit the event even for a shell pane, which is
why this looks fine when exercised through the API and broken when a human uses
the keyboard.)

So focus is polled every 0.5s: one `session.snapshot`, and nothing else is
fetched unless the focused pane actually changed. The actions additionally write
the pivot themselves the moment they move, because waiting for the next poll
would make a quick second press read a record that has not caught up — and
`back` is a toggle, so pressing it twice in a row is ordinary use.

### Why the marker rides on workspace metadata

herdr treats `workspace.report_metadata` as changing the UI and redraws the
sidebar for it. `pane.report_metadata` gets no such treatment. A marker that only
moves between agent rows would therefore update its token and then sit unseen
until something *else* forced a redraw — a keypress, or another plugin's periodic
workspace update. With a plugin like
[space-usage](https://github.com/ezcorp-org/herdr-pc-ram-and-cpu-usage-overlay)
installed that update lands every five seconds, which is exactly what the lag
looks like.

So the marker's location is also reported as workspace metadata under an
invisible `markerat` token. Every marker move then rides along with a workspace
update and the redraw comes with it — about 130ms instead of up to five seconds.
Only the workspace gaining the marker and the one losing it change value, so it
costs at most two extra requests per move.

Agent rows are stamped **before** the spaces row for the same reason: the spaces
stamp is what triggers the redraw, so the pane tokens have to already carry the
new marker position when it goes out.

### A note on `agent.focus`

Switching calls `pane.focus`, not `agent.focus`, even though it is moving between
agents. `agent.focus` does not take a pane id: given one it answers **success**
and then moves focus to an unrelated pane that is not even in the agent list.
Passing `terminal_id` instead is rejected with `agent_not_found`. Since
`agent.list` already hands out pane ids and `pane.focus` follows a pane across
spaces, that is both the correct and the simpler call.

### The agent list shrinks silently

A pane leaves the agent list the moment its agent exits, and nothing subscribable
announces it: `pane.exited` is the pane itself dying, not the agent inside it,
and `pane.agent_status_changed` requires a `pane_id` so it cannot be subscribed
for "any pane". Left stale, the set still lists a pane that is now an ordinary
shell — moving there would look like "moved to an agent" and hide the marker for
good. So the set is refreshed from the server on every focus change.

## Troubleshooting

The daemon logs focus changes, reconnects, and exceptions to its state
directory. It is truncated on every start, so it stays small:

```bash
cat ~/.local/state/herdr/plugins/newro.agent-nav/daemon.log
```

Each focus change appears as
`focus <pane> (agent=true/false) marker <before> -> <after>`. If the marker sits
in the wrong place, that line says what the daemon believed at the time. An empty
log with a live process means no focus change has been observed yet.

`herdr plugin log list --plugin newro.agent-nav` shows action invocations and
their exit codes.

## Development

```bash
herdr plugin link /path/to/herdr-agent-nav
python3 -m py_compile herdr_api.py daemon.py cycle.py   # syntax check
python3 cycle.py next --dry-run                        # decide without moving
python3 daemon.py --daemon                             # run in the foreground
```

`--dry-run` prints the pivot it chose, the agent list, and the target, which is
usually enough to tell a wrong pivot from a wrong move.

## Compatibility

`min_herdr_version` is `0.8.0`. Every API used — `events.subscribe`, the `tokens`
field on `pane.report_metadata` / `workspace.report_metadata`, `agent.list`,
`workspace.list`, `session.snapshot`, `pane.focus` — appears older than that in
the bundled schemas, but this was developed and verified against
`0.9.0-preview`. Treat the floor as conservative rather than tested, and lower it
only with a schema diff in hand.

## License

MIT
