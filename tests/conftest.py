import json
import os
import sys
import time
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentwatch import paths, store  # noqa: E402

PROMPT = {"type": "user", "message": {"role": "user", "content": "do the thing"}}
END_TURN = {"type": "assistant", "message": {"stop_reason": "end_turn", "content": []}}
HANDBACK = {
    "type": "user",
    "toolEndsTurn": True,
    "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]},
}


def tool_use(name, **args):
    return {
        "type": "assistant",
        "message": {
            "stop_reason": "tool_use",
            "content": [{"type": "tool_use", "name": name, "input": args}],
        },
    }


def stamped(row, at):
    """A transcript entry written at epoch `at`, as Claude Code stamps it."""
    return dict(row, timestamp=datetime.fromtimestamp(at, timezone.utc).isoformat().replace("+00:00", "Z"))


def write_rows(path, *rows, age=0.0):
    """Write a transcript. Each write gets a distinct mtime, so the transcript
    cache, keyed on (mtime, size), never serves a stale answer."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    write_rows.tick += 1
    stamp = time.time() - age + write_rows.tick * 1e-3
    os.utime(path, (stamp, stamp))
    return path


write_rows.tick = 0


@pytest.fixture
def events(tmp_path, monkeypatch):
    """An empty event log, and a helper that appends one event for session S
    owned by this (live) process."""
    monkeypatch.setattr(paths, "EVENTS", tmp_path / "events.jsonl")
    session = write_rows(tmp_path / "sess.jsonl", PROMPT)

    def emit(name, sid="S", **extra):
        event = dict(tool="claude", session=sid, event=name, pid=os.getpid(), transcript=str(session))
        store.append(dict(event, **extra))

    emit.transcript = session
    emit.agents = tmp_path / "sess" / "subagents"
    return emit
