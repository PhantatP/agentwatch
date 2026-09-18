"""Filesystem locations. Runtime state lives outside OneDrive to avoid sync churn."""
import os
from pathlib import Path

HOME = Path(os.path.expanduser("~"))
CLAUDE_HOME = Path(os.environ.get("CLAUDE_CONFIG_DIR") or HOME / ".claude")
CODEX_HOME = Path(os.environ.get("CODEX_HOME") or HOME / ".codex")

CLAUDE_PROJECTS = CLAUDE_HOME / "projects"
CODEX_SESSIONS = CODEX_HOME / "sessions"

DATA = Path(os.environ.get("LOCALAPPDATA") or HOME) / "agentwatch"
EVENTS = DATA / "events.jsonl"
USAGE_CACHE = DATA / "usage_cache.json"
TRAY_LOCK = DATA / "tray.pid"
LOG = DATA / "agentwatch.log"


def ensure_data() -> Path:
    DATA.mkdir(parents=True, exist_ok=True)
    return DATA
