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
import re
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


def latest(path: str):
    """-> (state, when): what the newest deciding entry says, and when it was
    written. (None, None) if the transcript cannot tell."""
    state, _, when = _summary(path)
    return state, when


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


# A launch heard nothing from for this long is presumed gone -- its CLI was
# restarted, say -- so it cannot hold a session's "done" back forever.
PENDING_MAX = 3600
_TASK_ID = re.compile(r"<task-id>([^<]+)</task-id>")
_STATUS = re.compile(r"<status>([^<]+)</status>")
_HANDBACK = re.compile(r'<agent-message from=\\*"([^"\\]+)')
_STILL_GOING = ("running", "pending", "in_progress")
# Where an ending is reported: a message, or -- while the main agent is busy --
# a queued command. Not api-request-blob, which replays old history and would
# clear an agent a later message has resumed.
_REPORTS = ("user", "queue-operation", "attachment")
_background: dict = {}  # transcript path -> incremental scan state


def _endings(line: str):
    """Task ids a line reports as ended: notifications with a final status,
    and agents' hand-backs."""
    for chunk in line.split("<task-notification>")[1:]:
        task, status = _TASK_ID.search(chunk), _STATUS.search(chunk)
        if task and status and status.group(1).strip() not in _STILL_GOING:
            yield task.group(1).strip()
    yield from _HANDBACK.findall(line)


def _scan_background(state: dict, obj: dict, line: str) -> None:
    if obj.get("type") in _REPORTS:
        for task in _endings(line):
            state["tasks"].pop(task, None)
        origin = obj.get("origin")
        if isinstance(origin, dict) and origin.get("kind") == "peer":
            state["tasks"].pop(origin.get("from"), None)  # a hand-back: it finished
    message = obj.get("message")
    if not isinstance(message, dict):
        return
    content = message.get("content")
    when = min(_entry_time(obj), time.time())
    if obj.get("type") == "assistant" and isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                args = block.get("input") or {}
                label = (
                    args.get("description")
                    or args.get("summary")
                    or block.get("name")
                    or "task"
                )
                state["uses"][block.get("id")] = (
                    block.get("name"),
                    str(label),
                    # whom it acts on: a message's agent, a stopped task
                    args.get("to") or args.get("task_id") or args.get("shell_id"),
                )
        return
    if obj.get("type") != "user" or not isinstance(content, list):
        return
    result = obj.get("toolUseResult")
    if not isinstance(result, dict):
        result = {}
    for block in content:
        if not (isinstance(block, dict) and block.get("type") == "tool_result"):
            continue
        name, label, to = state["uses"].pop(
            block.get("tool_use_id"), (None, "task", None)
        )
        task = result.get("backgroundTaskId") or result.get("taskId")
        if result.get("isAsync") and result.get("agentId"):
            task = result["agentId"]
            state["agents"][task] = label
        if task:
            state["tasks"][str(task)] = (label, when)
        elif name == "TaskStop":  # stopped tasks send no notification
            state["tasks"].pop(to, None)
        elif name == "SendMessage" and result.get("success") and to in state["agents"]:
            # A message to a finished agent resumes it, and it reports again.
            state["tasks"][to] = (state["agents"][to], when)


def background(path: str) -> dict:
    """Background work a session launched and has not heard back from, as
    {task id: label}: shells and monitors run in the background, async agents,
    and agents resumed by a message. Each one wakes the main agent when it
    ends, so until all have, a Stop is a pause, not the end.

    Read incrementally: only lines appended since the last call are parsed."""
    if not path:
        return {}
    try:
        size = os.path.getsize(path)
    except OSError:
        return {}
    state = _background.get(path)
    if state is None or size < state["offset"]:
        state = _background[path] = {"offset": 0, "uses": {}, "tasks": {}, "agents": {}}
    if size > state["offset"]:
        try:
            with open(path, "rb") as fh:
                fh.seek(state["offset"])
                data = fh.read()
        except OSError:
            data = b""
        cut = data.rfind(b"\n")
        if cut != -1:
            state["offset"] += cut + 1
            for line in data[: cut + 1].decode("utf-8", "replace").splitlines():
                if not any(
                    k in line
                    for k in ('"message"', "<task-notification>", "<agent-message")
                ):
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    _scan_background(state, obj, line)
    cutoff = time.time() - PENDING_MAX
    return {
        task: label for task, (label, when) in state["tasks"].items() if when >= cutoff
    }
