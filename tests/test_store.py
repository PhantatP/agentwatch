from agentwatch import store
from conftest import END_TURN, HANDBACK, PROMPT, tool_use, write_rows


def test_live_session_survives_reconcile(events):
    sessions = store.Sessions()
    events("UserPromptSubmit")
    sessions.poll()
    sessions.reconcile()
    assert sessions.aggregate() == "running"


def test_session_with_dead_process_is_dropped(events):
    sessions = store.Sessions()
    events("UserPromptSubmit", pid=0x7FFFFFF0)  # overrides the live default
    sessions.poll()
    sessions.reconcile()
    assert sessions.aggregate() == "none"


def test_plain_stop_notifies(events):
    sessions = store.Sessions()
    events("UserPromptSubmit")
    assert sessions.poll() == []
    events("Stop")
    assert [ev["event"] for ev, _ in sessions.poll()] == ["Stop"]


def test_stop_is_silent_until_the_last_agent_finishes(events):
    sessions = store.Sessions()
    events("UserPromptSubmit")
    write_rows(events.agents / "agent-a.jsonl", PROMPT, tool_use("Bash"))
    write_rows(events.agents / "agent-b.jsonl", PROMPT)

    # the main agent stops to wait on both
    events("Stop")
    assert sessions.poll() == []
    sessions.reconcile()
    assert sessions.aggregate() == "running"
    assert sessions.agent_count() == 2

    # a finishes; its result wakes the main agent, which stops again
    write_rows(events.agents / "agent-a.jsonl", PROMPT, END_TURN)
    events("UserPromptSubmit")
    events("Stop")
    assert sessions.poll() == []
    assert sessions.agent_count() == 1

    # b hands back; the main agent wraps up -- now it is done
    write_rows(events.agents / "agent-b.jsonl", PROMPT, HANDBACK)
    events("UserPromptSubmit")
    events("Stop")
    assert len(sessions.poll()) == 1
    sessions.reconcile()
    assert sessions.aggregate() == "idle"
    assert sessions.agent_count() == 0


def test_session_reads_running_while_agents_work(events):
    sessions = store.Sessions()
    events("Stop")
    sessions.poll()
    write_rows(events.agents / "agent-a.jsonl", PROMPT)
    sessions.reconcile()
    (sess,) = sessions.active()
    assert sess.status == "idle"
    assert sess.state == "running"
