"""Background work holds a session's "done" until it has all reported back."""

import json
import os
import time

from agentwatch import limits, paths, proc, store, transcript, tray, usage, winui
from conftest import PROMPT, stamped, write_rows


def launch(use_id, name, result, **args):
    """A tool call and its result, as Claude Code writes them."""
    call = {
        "type": "assistant",
        "message": {
            "content": [{"type": "tool_use", "id": use_id, "name": name, "input": args}]
        },
    }
    reply = {
        "type": "user",
        "toolUseResult": result,
        "message": {
            "content": [{"type": "tool_result", "tool_use_id": use_id, "content": "ok"}]
        },
    }
    return [stamped(call, time.time()), stamped(reply, time.time())]


def notification(task, status="completed"):
    text = (
        "<task-notification>\n<task-id>%s</task-id>\n<status>%s</status>\n</task-notification>"
        % (task, status)
    )
    return {
        "type": "user",
        "origin": {"kind": "task-notification"},
        "message": {"content": text},
    }


def append(path, *rows):
    """Append, as the CLI does -- the scan reads only what was added."""
    with open(path, "a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


SHELL = launch(
    "u1",
    "Bash",
    {"backgroundTaskId": "b1"},
    description="Run the build",
    run_in_background=True,
)
AGENT = launch(
    "u2", "Agent", {"isAsync": True, "agentId": "a1"}, description="Review the fix"
)


def test_background_shell_is_pending_until_its_notification(tmp_path):
    path = write_rows(tmp_path / "t.jsonl", PROMPT, *SHELL)
    assert transcript.background(str(path)) == {"b1": "Run the build"}
    append(path, notification("b1", "running"))
    assert transcript.background(str(path)) == {"b1": "Run the build"}  # not final yet
    append(path, notification("b1"))
    assert transcript.background(str(path)) == {}


def test_a_queued_notification_counts_too(tmp_path):
    """While the main agent is busy, an ending arrives as a queued command."""
    path = write_rows(tmp_path / "t.jsonl", PROMPT, *SHELL)
    text = "<task-notification>\n<task-id>b1</task-id>\n<status>failed</status>"
    append(path, {"type": "queue-operation", "operation": "enqueue", "content": text})
    assert transcript.background(str(path)) == {}


def test_replayed_history_does_not_clear_a_resumed_agent(tmp_path):
    path = write_rows(tmp_path / "t.jsonl", PROMPT, *AGENT)
    append(path, notification("a1"))
    assert transcript.background(str(path)) == {}
    append(
        path,
        *launch(
            "u3", "SendMessage", {"success": True}, to="a1", summary="One more thing"
        ),
    )
    assert transcript.background(str(path)) == {"a1": "Review the fix"}  # resumed
    blob = {
        "type": "api-request-blob",
        "content": "<task-notification><task-id>a1</task-id><status>completed</status>",
    }
    append(path, blob)
    assert transcript.background(str(path)) == {"a1": "Review the fix"}
    handback = {
        "type": "user",
        "origin": {"kind": "peer", "from": "a1"},
        "message": {"content": "report"},
    }
    append(path, handback)
    assert transcript.background(str(path)) == {}


def test_a_stopped_task_is_no_longer_pending(tmp_path):
    path = write_rows(tmp_path / "t.jsonl", PROMPT, *SHELL)
    append(path, *launch("u4", "TaskStop", {"message": "stopped"}, task_id="b1"))
    assert transcript.background(str(path)) == {}


def test_a_launch_silent_for_an_hour_is_presumed_gone(tmp_path, monkeypatch):
    path = write_rows(tmp_path / "t.jsonl", PROMPT, *SHELL)
    later = time.time() + transcript.PENDING_MAX + 1
    monkeypatch.setattr(time, "time", lambda: later)
    assert transcript.background(str(path)) == {}


def test_stop_while_a_background_task_runs_is_not_done(events):
    append(events.transcript, *SHELL)
    sessions = store.Sessions()
    events("UserPromptSubmit")
    events("Stop")
    assert sessions.poll() == []  # silent: the build is still running
    assert sessions.aggregate() == "running"
    append(events.transcript, notification("b1"))
    events("UserPromptSubmit")  # the notification wakes the main agent
    events("Stop")
    assert [ev["event"] for ev, _s in sessions.poll()] == ["Stop"]  # now it is done


def test_held_done_is_dropped_when_background_work_appears(events, monkeypatch):
    t = tray.Tray()
    t.first_poll, said = False, []
    monkeypatch.setattr(t, "announce", lambda ev, sess: said.append(ev["event"]))
    events("UserPromptSubmit")
    events("Stop")
    t.tick()
    append(events.transcript, *SHELL)  # launched just as the turn ended
    t.sessions.reconcile()
    monkeypatch.setattr(
        time, "time", lambda real=time.time: real() + tray.DONE_GRACE + 1
    )
    t.tick()
    assert said == []


# ---------- usage panel data ----------


def test_cached_limits_show_at_startup_without_reset_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "LIMITS_CACHE", tmp_path / "limits.json")
    now = time.time()
    windows = [
        {"label": "5h block", "percent": 40.0, "resets_at": now + 3600},
        {"label": "old", "percent": 99.0, "resets_at": now - 60},  # reset since
    ]
    (tmp_path / "limits.json").write_text(
        json.dumps({"windows": windows, "fetched_at": now - 30})
    )
    loaded = limits.ClaudeLimits()
    assert [w["label"] for w in loaded.windows] == ["5h block"]
    assert loaded.fetched_at == now - 30


def test_tokens_split_by_model_family():
    assert usage.model_family("claude-opus-5-5") == "Opus"
    assert usage.model_family("claude-haiku-5-5") == "Haiku"
    now = time.time()
    entries = [
        [now, 100, 0, 0, 0, 0, "claude-opus-5-5", "1"],
        [now, 50, 0, 0, 0, 0, "claude-sonnet-5-5", "2"],
        [now, 25, 0, 0, 0, 0, "claude-opus-5-5", "3"],
        [now, 0, 0, 0, 0, 0, "<synthetic>", "4"],
    ]
    today = time.strftime("%Y-%m-%d")
    assert usage._by_model(entries, [today]) == {"Opus": 125, "Sonnet": 50}


def test_hidden_app_leaves_the_tooltip():
    t = tray.Tray()
    t.claude_limits.windows = [{"label": "5h block", "percent": 40.0, "resets_at": time.time() + 600}]
    t.tick()
    assert "5h block 40%" in t.icon.title
    t.settings["show_claude"] = False
    t.tick()
    assert "5h block" not in t.icon.title

def test_only_idle_agent_cli_sessions_can_be_ended(events):
    t = tray.Tray()
    events("UserPromptSubmit", sid="busy")
    events("Stop", sid="done")
    events("Stop", sid="ide", tool="antigravity")
    cli = r"C:\Users\u\.gemini\antigravity-cli\brain\cli\transcript_full.jsonl"
    events("Stop", sid="cli", tool="antigravity", transcript=cli)
    t.sessions.poll()
    assert sorted(s.sid for s in t.idle_sessions()) == ["cli", "done"]


def test_ending_asks_first_and_refuses_a_pid_that_is_not_an_agent(events, monkeypatch):
    t = tray.Tray()
    events("Stop")
    t.sessions.poll()
    ended = []
    monkeypatch.setattr(proc, "end_tree", lambda pid: ended.append(pid) or True)
    monkeypatch.setattr(winui, "confirm", lambda title, text: False)
    t._end(t.idle_sessions())
    assert ended == []  # you said no
    monkeypatch.setattr(winui, "confirm", lambda title, text: True)
    t._end(t.idle_sessions())
    assert ended == [os.getpid()]
    assert t.sessions.active() == []


def test_end_tree_will_not_kill_a_non_agent_process():
    assert proc.end_tree(os.getpid()) is False  # this is python.exe, not an agent CLI


def test_panel_offers_end_only_for_idle_sessions(events):
    t = tray.Tray()
    events("UserPromptSubmit", sid="busy")
    events("Stop", sid="done")
    t.tick()
    assert {r["sess"].sid: r["can_end"] for r in t.view} == {"busy": False, "done": True}
