"""Decide whether a session is really mid-turn by reading its transcript tail.

Hook events alone are not enough: Claude Code emits no `Stop` when you interrupt
with Esc, and neither CLI emits anything if the terminal is killed, so a session
that stopped can stay stuck in `running` forever.

The transcript settles it. A completed turn ends with an assistant message whose
`stop_reason` is `end_turn`; a turn still in flight ends with `tool_use` or with a
user tool_result. An interrupt is written as a user message containing
`[Request interrupted by user]`.
"""
import json
import os

TAIL_BYTES = 64 * 1024
INTERRUPT_MARKERS = ("Request interrupted", "[Request interrupted by user]")

_cache: dict = {}  # path -> (mtime, size, state)


def _tail(path: str) -> str:
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        if size > TAIL_BYTES:
            fh.seek(size - TAIL_BYTES)
            fh.readline()  # discard the partial first line
        return fh.read().decode("utf-8", "replace")


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(str(block.get("text") or block.get("content") or ""))
        return " ".join(parts)
    return ""


def _state_from_text(text: str):
    """-> 'running' | 'idle' | None (undetermined)."""
    for line in reversed(text.splitlines()):
        if '"message"' not in line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = obj.get("type")
        if kind not in ("assistant", "user"):
            continue
        message = obj.get("message")
        if not isinstance(message, dict):
            continue
        if kind == "assistant":
            # end_turn means the turn finished; tool_use means it is still going.
            return "idle" if message.get("stop_reason") == "end_turn" else "running"
        body = _text_of(message.get("content"))
        if any(marker in body for marker in INTERRUPT_MARKERS):
            return "idle"
        return "running"  # a prompt or a tool result: the agent is working
    return None


def turn_state(path: str):
    """Cached per (mtime, size) so a 1s poll costs nothing when nothing changed."""
    if not path:
        return None
    try:
        stat = os.stat(path)
    except OSError:
        return None
    key = (stat.st_mtime, stat.st_size)
    cached = _cache.get(path)
    if cached and cached[0] == key:
        return cached[1]
    try:
        state = _state_from_text(_tail(path))
    except OSError:
        return None
    _cache[path] = (key, state)
    return state
