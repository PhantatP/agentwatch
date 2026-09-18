"""Real usage percentages, the same ones `/usage` and `/status` show.

Claude keeps these server-side only -- nothing on disk holds a utilization
figure -- so this calls the endpoint Claude Code itself calls,
`/api/oauth/usage`, reusing the OAuth token Claude Code already stored. It is a
metadata request, not inference, so it consumes no model tokens.

Codex needs no call at all: its server attaches a `rate_limits` snapshot to every
API response, which lands in the session transcript. That snapshot is exact but
only as fresh as the last Codex turn, so it is reported with its age.

If the stored token has expired, this reports that instead of trying to refresh
it -- refreshing would mean writing to the credentials file Claude Code owns.
"""
import json
import time
import urllib.error
import urllib.request
from datetime import datetime

from . import paths

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
REFRESH_SECONDS = 300
TIMEOUT = 15

# `kind` from the endpoint -> the label shown in the menu.
KIND_LABELS = {
    "session": "5h block",
    "weekly_all": "week",
    "weekly_opus": "week (Opus)",
    "weekly_sonnet": "week (Sonnet)",
    "spend": "spend",
}


def _token() -> str:
    """Read fresh every call: Claude Code rewrites this file when it refreshes."""
    with open(paths.CLAUDE_HOME / ".credentials.json", encoding="utf-8") as fh:
        return json.load(fh)["claudeAiOauth"]["accessToken"]


def _parse_reset(value) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def fetch_claude() -> dict:
    """-> {"windows": [{label, percent, resets_at, severity}], "fetched_at": ts}"""
    request = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": "Bearer " + _token(),
            "anthropic-beta": "oauth-2025-04-20",
            "Content-Type": "application/json",
            "User-Agent": "agentwatch (local usage indicator)",
        },
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        payload = json.loads(response.read().decode("utf-8"))

    windows = []
    for entry in payload.get("limits") or []:
        if not isinstance(entry, dict) or entry.get("percent") is None:
            continue
        kind = entry.get("kind") or ""
        windows.append(
            {
                "label": KIND_LABELS.get(kind, kind.replace("_", " ")),
                "percent": float(entry["percent"]),
                "resets_at": _parse_reset(entry.get("resets_at")),
                "severity": entry.get("severity") or "normal",
            }
        )
    if not windows:  # older shape, before `limits` existed
        for key, label in (("five_hour", "5h block"), ("seven_day", "week")):
            block = payload.get(key)
            if isinstance(block, dict) and block.get("utilization") is not None:
                windows.append(
                    {
                        "label": label,
                        "percent": float(block["utilization"]),
                        "resets_at": _parse_reset(block.get("resets_at")),
                        "severity": "normal",
                    }
                )
    return {"windows": windows, "fetched_at": time.time()}


def codex_windows(limits: dict) -> list:
    """Turn a stored Codex rate_limits snapshot into the same window shape."""
    if not isinstance(limits, dict):
        return []
    windows = []
    for slot in ("primary", "secondary"):
        window = limits.get(slot)
        if not isinstance(window, dict) or window.get("used_percent") is None:
            continue
        minutes = window.get("window_minutes") or 0
        if minutes >= 1440:
            label = "%dd" % (minutes // 1440)
        elif minutes:
            label = "%dh" % (minutes // 60)
        else:
            label = slot
        windows.append(
            {
                "label": label,
                "percent": float(window["used_percent"]),
                "resets_at": float(window.get("resets_at") or 0),
                "severity": "normal",
                "as_of": float(limits.get("_ts") or 0),
            }
        )
    return windows


class ClaudeLimits:
    """Polls the endpoint at most every REFRESH_SECONDS, keeping the last good value."""

    def __init__(self):
        self.windows = []
        self.fetched_at = 0.0
        self.error = ""

    def maybe_refresh(self, force: bool = False) -> None:
        if not force and time.time() - self.fetched_at < REFRESH_SECONDS:
            return
        try:
            result = fetch_claude()
            self.windows = result["windows"]
            self.fetched_at = result["fetched_at"]
            self.error = ""
        except urllib.error.HTTPError as exc:
            self.error = "sign in to Claude Code" if exc.code in (401, 403) else "HTTP %d" % exc.code
        except (OSError, ValueError, KeyError) as exc:
            self.error = type(exc).__name__


def bar(percent: float, width: int = 8) -> str:
    """A dot meter. Every glyph has the same advance width in Segoe UI, so rows
    stay aligned in the proportional font Windows uses for tray menus."""
    filled = max(0, min(width, int(round(percent / 100.0 * width))))
    if percent > 0 and filled == 0:
        filled = 1  # never render real usage as empty
    return "●" * filled + "○" * (width - filled)


def age(ts: float) -> str:
    if not ts:
        return "never"
    seconds = max(0, int(time.time() - ts))
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return "%dm ago" % (seconds // 60)
    if seconds < 86400:
        return "%dh ago" % (seconds // 3600)
    return "%dd ago" % (seconds // 86400)
