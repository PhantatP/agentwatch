import json

from agentwatch import transcript
from conftest import END_TURN, HANDBACK, PROMPT, tool_use, write_rows


def test_turn_states(tmp_path):
    path = tmp_path / "t.jsonl"
    assert transcript.turn_state(str(write_rows(path, PROMPT))) == "running"
    assert transcript.turn_state(str(write_rows(path, PROMPT, tool_use("Bash")))) == "running"
    assert transcript.turn_state(str(write_rows(path, PROMPT, END_TURN))) == "idle"
    interrupted = {"type": "user", "message": {"content": "[Request interrupted by user]"}}
    assert transcript.turn_state(str(write_rows(path, PROMPT, interrupted))) == "idle"


def test_handback_ends_a_subagent_turn(tmp_path):
    path = write_rows(tmp_path / "t.jsonl", PROMPT, tool_use("SubagentHandback"), HANDBACK)
    assert transcript.turn_state(str(path)) == "idle"


def test_missing_transcript_is_undetermined(tmp_path):
    assert transcript.turn_state(str(tmp_path / "nope.jsonl")) is None
    assert transcript.activity("") == ""


def test_activity_names_the_tool_and_its_target(tmp_path):
    path = tmp_path / "t.jsonl"
    edit = tool_use("Edit", file_path=r"C:\repo\agentwatch\tray.py", old_string="a")
    assert transcript.activity(str(write_rows(path, PROMPT, edit))) == "Edit tray.py"
    bash = tool_use("Bash", command="pytest -q", description="Run tests")
    assert transcript.activity(str(write_rows(path, PROMPT, bash))) == "Bash Run tests"
    assert transcript.activity(str(write_rows(path, PROMPT, tool_use("TaskList")))) == "TaskList"


def test_activity_between_tools_and_when_done(tmp_path):
    path = tmp_path / "t.jsonl"
    result = {"type": "user", "message": {"content": [{"type": "tool_result", "content": "x"}]}}
    assert transcript.activity(str(write_rows(path, PROMPT, tool_use("Read"), result))) == "thinking"
    writing = {"type": "assistant", "message": {"stop_reason": None, "content": [{"type": "text", "text": "Hi"}]}}
    assert transcript.activity(str(write_rows(path, PROMPT, writing))) == "writing"
    assert transcript.activity(str(write_rows(path, PROMPT, END_TURN))) == ""


def test_agent_label_from_meta(tmp_path):
    path = tmp_path / "agent-a1.jsonl"
    meta = {"agentType": "reviewer", "description": "Falsify core.py fixes"}
    (tmp_path / "agent-a1.meta.json").write_text(json.dumps(meta), encoding="utf-8")
    assert transcript.agent_label(str(path)) == "reviewer · Falsify core.py fixes"
    assert transcript.agent_label(str(tmp_path / "agent-none.jsonl")) == "agent"


def test_live_agents_skips_finished_and_silent_ones(tmp_path):
    main = tmp_path / "sess.jsonl"
    folder = tmp_path / "sess" / "subagents"
    write_rows(folder / "agent-busy.jsonl", PROMPT, tool_use("Bash"))
    write_rows(folder / "agent-done.jsonl", PROMPT, END_TURN)
    write_rows(folder / "agent-handed.jsonl", PROMPT, HANDBACK)
    write_rows(folder / "agent-killed.jsonl", PROMPT, age=transcript.AGENT_QUIET + 60)
    (folder / "agent-busy.meta.json").write_text("{}", encoding="utf-8")
    live = transcript.live_agents(str(main))
    assert list(live) == ["busy"]
    assert live["busy"].endswith("agent-busy.jsonl")


def test_live_agents_without_a_subagents_folder(tmp_path):
    assert transcript.live_agents(str(tmp_path / "sess.jsonl")) == {}
    assert transcript.live_agents("") == {}
