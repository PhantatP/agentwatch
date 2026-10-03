"""Is each tool's hook wiring actually working? Shown in the tray's HOOKS section.

A hook fails silently by design -- it must never break the agent's turn -- so a
broken one looks exactly like a tool nobody is using. Codex did just that: it
loaded our hooks but first would not run them (untrusted), then could not
(PowerShell rejected the quoted command). The tell is a tool whose transcripts
keep changing while its hooks stay quiet.
"""
import glob
import os
import time

from . import paths

# A tool used this long after its last hook event is not firing its hooks.
# Every turn fires at its start and its end, so only a single turn running
# longer than this could trip it falsely.
SILENT_AFTER = 2 * 3600
# A tool without our hooks, unused this long, is not worth a row.
FORGOTTEN_AFTER = 30 * 86400


def _newest(pattern: str) -> float:
    newest = 0.0
    for path in glob.glob(pattern):
        try:
            newest = max(newest, os.path.getmtime(path))
        except OSError:
            continue
    return newest


def _codex_newest() -> float:
    """Codex files sessions by date, sessions/YYYY/MM/DD -- only the latest day
    needs a look, however many months have piled up."""
    folder = paths.CODEX_SESSIONS
    for _ in range(3):
        try:
            names = sorted(n for n in os.listdir(folder) if n.isdigit())
        except OSError:
            return 0.0
        if not names:
            return 0.0
        folder = folder / names[-1]
    return _newest(str(folder / "*.jsonl"))


# tool -> (its hook config, when it was last used)
TOOLS = {
    "claude": (
        lambda: paths.CLAUDE_HOME / "settings.json",
        lambda: _newest(str(paths.CLAUDE_PROJECTS / "*" / "*.jsonl")),
    ),
    "codex": (lambda: paths.CODEX_HOME / "hooks.json", _codex_newest),
    "antigravity": (
        lambda: paths.GEMINI_HOME / "config" / "hooks.json",
        lambda: _newest(str(paths.GEMINI_HOME / "antigravity" / "conversations" / "*.pb")),
    ),
}

# Where to look when a tool's hooks are installed but silent.
HINTS = {"codex": "check trust in codex /hooks"}


def _installed(config) -> bool:
    try:
        return "agentwatch" in config.read_text(encoding="utf-8")
    except OSError:
        return False


def check(last_event: dict, ago) -> list:
    """-> [(tool, status text, healthy)] for every tool installed or in use.
    `last_event` maps tool -> time of its newest hook event; `ago` formats one."""
    rows = []
    for tool, (config, last_used) in TOOLS.items():
        installed, used = _installed(config()), last_used()
        if not installed and time.time() - used > FORGOTTEN_AFTER:
            continue  # never used, or left behind by an uninstalled tool
        seen = last_event.get(tool, 0.0)
        if not installed:
            rows.append((tool, "hooks not installed · run install.py", False))
        elif used and used - seen > SILENT_AFTER:
            text = "hooks not firing · used %s, last event %s" % (ago(used), ago(seen))
            hint = HINTS.get(tool)
            rows.append((tool, text + (" · " + hint if hint else ""), False))
        else:
            rows.append((tool, "last event %s" % ago(seen) if seen else "no events yet", True))
    return rows


def log_errors() -> int:
    """Lines in agentwatch.log -- it only ever holds errors."""
    try:
        with open(paths.LOG, encoding="utf-8", errors="replace") as fh:
            return sum(1 for line in fh if line.strip())
    except OSError:
        return 0
