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
import time
from datetime import datetime

TAIL_BYTES = 64 * 1024
AGENT_QUIET = 15 * 60  # longer than the 10-minute cap on a single tool call
INTERRUPT_MARKERS = ("Request interrupted", "[Request interrupted by user]")

_cache: dict = {}  # path -> ((mtime, size), (state, activity, when))


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


def _tool_label(block: dict) -> str:
    """'Edit tray.py', 'Bash Run tests' -- the tool, plus what it is acting on."""
    args = block.get("input") or {}
    detail = (
        os.path.basename(str(args.get("file_path") or ""))
        or args.get("description")
        or args.get("pattern")
        or ""
    )
    name = block.get("name") or "tool"
    return "%s %s" % (name, detail) if detail else name


def _activity(obj: dict) -> str:
    """What the agent is doing, judged from its latest transcript entry."""
    content = obj["message"].get("content")
    if obj.get("type") == "assistant" and isinstance(content, list):
        for block in reversed(content):
            if isinstance(block, dict) and block.get("type") == "tool_use":
                return _tool_label(block)
            if isinstance(block, dict) and block.get("type") == "text":
                return "writing"
    return "thinking"  # mid-reasoning, or waiting on the model after a tool result


def _entry_time(obj) -> float:
    """When an entry was written, in epoch seconds. An unstamped one counts as
    new, so it is never dismissed as left over from an earlier turn."""
    try:
        return datetime.fromisoformat(obj["timestamp"].replace("Z", "+00:00")).timestamp()
    except (KeyError, TypeError, AttributeError, ValueError):
        return float("inf")


def _state_from_text(text: str):
    """-> (state, activity, when). State is 'running' | 'idle' | None
    (undetermined); activity is '' unless running; when is the deciding entry's
    time."""
    state, entry = _state_and_entry(text)
    if entry is None:
        return None, "", None
    return state, (_activity(entry) if state == "running" else ""), _entry_time(entry)


def _state_and_entry(text: str):
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
            return ("idle" if message.get("stop_reason") == "end_turn" else "running"), obj
        if obj.get("toolEndsTurn"):
            return "idle", obj  # a subagent's hand-back: its last act is a tool call
        body = _text_of(message.get("content"))
        if any(marker in body for marker in INTERRUPT_MARKERS):
            return "idle", obj
        return "running", obj  # a prompt or a tool result: the agent is working
    return None, None


def _summary(path: str):
    """-> (state, activity, when), cached per (mtime, size) so a 1s poll costs
    nothing when nothing changed."""
    if not path:
        return None, "", None
    try:
        stat = os.stat(path)
    except OSError:
        return None, "", None
    key = (stat.st_mtime, stat.st_size)
    cached = _cache.get(path)
    if cached and cached[0] == key:
        return cached[1]
    try:
        summary = _state_from_text(_tail(path))
    except OSError:
        return None, "", None
    _cache[path] = (key, summary)
    return summary


def turn_state(path: str):
    return _summary(path)[0]


def ended_at(path: str):
    """When the turn in this transcript ended, or None if it has not."""
    state, _, when = _summary(path)
    return when if state == "idle" else None


def activity(path: str) -> str:
    """What a running agent is doing right now, e.g. 'Edit tray.py'; '' if idle."""
    return _summary(path)[1]


_labels: dict = {}  # agent transcript path -> label; an agent's meta never changes


def agent_label(path: str) -> str:
    """'reviewer · Falsify core.py fixes', from the agent's sidecar meta file."""
    if path not in _labels:
        try:
            with open(path[: -len(".jsonl")] + ".meta.json", "r", encoding="utf-8") as fh:
                meta = json.load(fh)
        except (OSError, json.JSONDecodeError):
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        parts = [meta.get("agentType"), meta.get("description")]
        _labels[path] = " · ".join(str(p) for p in parts if p) or "agent"
    return _labels[path]


def live_agents(path: str) -> dict:
    """Subagents of a session that are still mid-turn, as {id: transcript path}.

    Read from their own transcripts, `<session>/subagents/agent-<id>.jsonl`,
    because hooks cannot track them: SubagentStart does not fire for background
    agents. One silent for AGENT_QUIET is presumed killed, not still working.
    """
    live = {}
    if not path:
        return live
    folder = os.path.join(os.path.splitext(path)[0], "subagents")
    cutoff = time.time() - AGENT_QUIET
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return live
    for entry in entries:
        name = entry.name
        if not (name.startswith("agent-") and name.endswith(".jsonl")):
            continue
        try:
            if entry.stat().st_mtime < cutoff:
                continue
        except OSError:
            continue
        if turn_state(entry.path) == "running":
            live[name[len("agent-"):-len(".jsonl")]] = entry.path
    return live
