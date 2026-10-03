# agentwatch

A tray indicator and notifier for Claude Code and Codex sessions on Windows.
It shows whether an agent is working, waiting on you, or idle, notifies you when
a turn ends, and reports token usage.

**It consumes no model tokens.** Everything comes from local hook events and the
transcript files both CLIs already write to disk. Nothing is sent anywhere.

## What you see

A single tray icon whose colour is the worst state across all live sessions:

Shape carries the state as well as colour, so it still reads at 16px and does
not depend on telling the hues apart:

| Icon | Colour | Meaning |
| --- | --- | --- |
| Open arc | Blue | An agent is working |
| Open arc with a centre dot | Blue | An agent is working, and its subagents are too |
| Filled disc | Amber | An agent needs you: a question, a permission prompt, or an idle wait |
| Ring | Green | Session open, turn finished |
| Thin outline | Grey | Nothing running |

The glyph is drawn at 8x and downsampled, so it stays crisp at every DPI. A
numbered badge appears when more than one session is live. Hover for a tooltip;
right-click for the full breakdown:

```
SESSIONS
  ● codex   my-ai-workflows · needs you · 8s
  ◐ claude  indicator · running · 1m12s · 2 agents
        Edit tray.py
        ↳ reviewer · Falsify core.py fixes — Bash Run tests
        ↳ Explore · Find hook wiring — thinking
────────────────────────────────────────────
CLAUDE
  ●●○○○○○○   28%   5h block · resets 4h04m
  ●○○○○○○○   13%   week · resets 5d
CODEX
  ●○○○○○○○    9%   7d · 36d ago
TOKENS
  today   claude 50.1M · codex 0
  7 days  claude 54.1M · codex 0
────────────────────────────────────────────
✓ Toasts
✓ Flash taskbar
  Refresh usage now
  Open data folder
  Quit
```

Only the section headers are disabled items. Windows renders disabled text in a
grey so light it is hard to read, so every row carrying real information stays
enabled and renders in normal text. Each meter leads its row because its glyphs
are all one width, which keeps the columns aligned in the proportional font
Windows uses for tray menus. Clicking a session flashes the terminal that owns
it; clicking a usage row refreshes.

Under a running session is what it is doing now — the tool it is in and what
that tool is acting on, or `thinking` / `writing` between tools — and one `↳`
row per live subagent, named from its type and task. All of it comes from the
transcript tails the tray already reads, so it adds nothing to hook latency.

## Notifications

On every turn end you get a Windows toast (which persists in the Action Center)
and a taskbar flash on the terminal window that owns the session — found by
walking up the process tree from the agent CLI, so it works under Windows
Terminal, pwsh, and VS Code alike.

Subagents do not notify. While a Claude session has subagents running it stays
blue, and the main agent's `Stop` — both when it pauses to wait and
when it wakes to read one agent's result — is silent. You get one "done" when
the last agent has finished and the main agent wraps up.

Nothing is shown when the window hosting the session is already in front — you
are looking at it. For VS Code that means any VS Code window, since they all
belong to one process.

A "done" is also held for 3 seconds and dropped if the session starts another
turn in that time: a queued message, or an agent's result arriving just as the
main agent stopped, means it was not really done.

| Event | Notification |
| --- | --- |
| `Stop` | "Finished" |
| `PermissionRequest` (Codex) | "Asking permission" |
| `Interrupt` (Codex) | "Interrupted" |

Neither CLI emits a distinct *error* event. A failed turn still ends in `Stop`,
and an aborted one in `Interrupt` (Codex only), so those two cover the endings
you can actually observe.

## Usage numbers

The tray shows the same percentages as `/usage` and `/status`, because it reads
the same sources they do.

| Number | Where it comes from |
| --- | --- |
| Claude 5h block %, week % | Live. Claude keeps these server-side only — no file on disk holds a utilization figure — so agentwatch calls `/api/oauth/usage`, the endpoint Claude Code's own `/usage` calls, reusing the OAuth token in `~/.claude/.credentials.json`. It is a metadata request, not inference, so it costs no model tokens. Refreshed every 5 minutes. |
| Codex limit % | Exact, but only as fresh as Codex's last turn. Codex's server attaches a `rate_limits` snapshot to every API response, which lands in the session transcript, so no call is needed. The age is shown beside it, e.g. `9% (36d ago)`. |
| Token totals (today, 7d) | Derived from transcripts, for both tools. These include cache reads, which is why a busy day reads in the tens of millions — that is not what your plan meters, which is why the percentages above are the numbers to trust. |

If the stored OAuth token has expired the menu says `sign in to Claude Code`
rather than attempting a refresh — refreshing would mean writing to a credentials
file Claude Code owns.

A rate-limited Codex turn writes a snapshot whose windows are all `null`;
agentwatch skips those and falls back to the most recent real one.

## Install

Requires Python 3.11+ and `pystray` (`pip install pystray`; Pillow comes with it).

```
python install.py
```

This writes hooks into `~/.claude/settings.json` and `~/.codex/hooks.json`,
backing up each file first. It only ever touches entries carrying its own
`--source agentwatch` marker, so your existing hooks are left alone, and
re-running it replaces rather than duplicates.

**Restart Claude Code and Codex afterwards** — both read hook config at startup.

The tray starts itself on the next agent turn: the `SessionStart` and
`UserPromptSubmit` hooks launch it (as `pythonw.exe`, so no console window) if it
is not already running, and a PID lock keeps it to one instance. It appears about
a second after you start a turn, and it does not run at login — nothing to see
when no agent is working. To start it by hand:

```
python -m agentwatch.tray
```

To remove everything:

```
python install.py --uninstall
```

Uninstall leaves the tray running; quit it from its menu.

## How it works

```
Claude Code / Codex
      | hook fires (SessionStart, UserPromptSubmit, Stop, ...)
      v
hook_entry.py  --  appends one JSON line, exits in ~110ms
      v
%LOCALAPPDATA%\agentwatch\events.jsonl
      v
tray.py  --  one long-lived process: derives state, draws the icon, notifies
      +  usage.py  --  incremental parse of ~/.claude/projects and ~/.codex/sessions
```

The split matters: a hook blocks the agent's turn while it runs, so hooks do the
minimum and the tray does the work. Transcript parsing is cached per file by size
and byte offset, so a refresh re-reads only what was appended — a cold scan of all
transcripts takes under a second, warm scans are instant.

Runtime state lives in `%LOCALAPPDATA%\agentwatch\`, deliberately outside
OneDrive, so it does not generate sync traffic:

| File | Purpose |
| --- | --- |
| `events.jsonl` | Hook event log, trimmed to the last 2000 lines |
| `usage_cache.json` | Per-file parse cache |
| `settings.json` | Toast and flash toggles |
| `tray.pid` | Single-instance lock |
| `agentwatch.log` | Errors only; normally empty |

## Tests

```
python -m pip install pytest
python -m pytest tests
```

The tests build sessions from a temporary event log and fake transcripts, so they
need no running CLI and never touch your real `%LOCALAPPDATA%\agentwatch`.

## Notes and limits

- Windows only. The taskbar flash and tray notifications are Win32-specific.
- Codex reads `~/.codex/hooks.json`. It does *not* read `~/.codex/hooks/hooks.json`
  (a plugin-only path) and ignores files placed there without warning. Its `command`
  must be a single string, not a list. Both facts were established against the
  app-server `hooks/list` RPC, which is also how to check the wiring at any time —
  it reports the hooks Codex actually loaded, with warnings and errors.
- A session that stops without an event — Esc, or a closed terminal — is caught by
  the tray rather than left stuck. It re-reads the transcript tail: a turn that
  ended writes `stop_reason: end_turn`, an interrupted one writes
  `[Request interrupted by user]`. It also drops any session whose process is gone.
- Subagents are read from their own transcripts, `<session>/subagents/agent-<id>.jsonl`,
  not from hooks: `SubagentStart` does not fire for background agents, which are
  the ones that matter. One is live while its tail says mid-turn — a finished one
  ends in `end_turn` or a tool result marked `toolEndsTurn`. One silent for 15
  minutes is presumed killed, so an agent lost to Esc cannot hold the icon blue.
- "Whose process" means the agent CLI, not the hook's direct parent. Claude Code
  runs each hook through a shell (`bash.exe` with Git Bash) that exits with the
  hook, so the hook walks past any shell ancestors and records the CLI's pid.
  Recording the shell made every session look dead within a second.
- Codex hooks are registered with `"async": true` so they never delay a turn.
  Claude Code hooks are synchronous and add roughly 130ms per event: Python
  interpreter startup plus the ~20ms process-tree walk.
- Sessions with no event for 12 hours are dropped from the tray, so a terminal
  closed without a clean `SessionEnd` does not leave a stale row.
