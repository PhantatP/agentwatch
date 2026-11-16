# agentwatch

A Windows tray indicator for Claude Code, Codex, and Antigravity sessions.

- The tray icon shows at a glance whether an agent is working, needs you, or
  has just finished.
- You get one Windows toast when an agent's whole job is done, including its
  subagents and background tasks.
- Clicking the icon opens a panel listing your sessions (with a button to end
  idle ones) and showing usage limits, token totals, a 7-day chart, and hook
  health.

It uses no model tokens and sends nothing anywhere: everything comes from hook
events and the transcript files the CLIs already write.

## Install

Requires Windows, Python 3.11+, and `pystray`:

```
pip install pystray
python install.py
```

`install.py` adds agentwatch's hooks to Claude Code, Codex, and (if installed)
Antigravity, backing up each config file first and leaving your other hooks
alone. Then:

1. Restart Claude Code, Codex, and Antigravity, so they load the hooks.
2. For Codex only: run `codex` in a terminal, type `/hooks`, and trust the
   agentwatch hooks. Codex skips untrusted hooks silently.

The tray starts by itself on your next agent turn. To start it by hand:
`python -m agentwatch.tray`.

## Uninstall

```
python install.py --uninstall
```

Then quit the tray from its panel.
