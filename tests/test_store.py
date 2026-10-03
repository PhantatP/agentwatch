import time

from agentwatch import store
from conftest import END_TURN, HANDBACK, PROMPT, stamped as _stamped, tool_use, write_rows


def test_live_session_survives_reconcile(events):
    sessions = store.Sessions()
    events("UserPromptSubmit")
    sessions.poll()
    sessions.reconcile()
    assert sessions.aggregate() == "running"


def test_new_turn_survives_the_previous_turns_end_turn(events):
    """Claude writes the prompt ~140ms after the UserPromptSubmit hook, so a
    reconcile in that gap sees the previous turn's end_turn. It must not count."""
    write_rows(events.transcript, PROMPT, _stamped(END_TURN, time.time() - 20))
    sessions = store.Sessions()
    events("UserPromptSubmit")
    sessions.poll()
    sessions.reconcile()
    assert sessions.aggregate() == "running"


def test_interrupt_after_the_turn_started_still_ends_it(events):
    sessions = store.Sessions()
    events("UserPromptSubmit")
    sessions.poll()
    interrupted = {"type": "user", "message": {"content": "[Request interrupted by user]"}}
    write_rows(events.transcript, PROMPT, _stamped(interrupted, time.time() + 1))
    sessions.reconcile()
    assert sessions.aggregate() == "idle"


def test_finished_turn_shows_green_briefly_then_goes_quiet(events, monkeypatch):
    sessions = store.Sessions()
    events("UserPromptSubmit")
    events("Stop")
    sessions.poll()
    assert sessions.aggregate() == "idle"
    later = time.time() + store.IDLE_FADE + 1
    monkeypatch.setattr(time, "time", lambda: later)
    assert sessions.aggregate() == "quiet"
    (sess,) = sessions.active()
    assert sess.status == "idle"  # still open and listed, just no longer news


def test_idle_reminder_does_not_turn_amber_but_permission_does(events):
    sessions = store.Sessions()
    events("UserPromptSubmit")
    events("Stop")
    events("Notification", message="Claude is waiting for your input")
    sessions.poll()
    assert sessions.aggregate() == "idle"
    events("UserPromptSubmit")
    events("Notification", message="Claude needs your permission to use Bash")
    sessions.poll()
    assert sessions.aggregate() == "waiting"


def test_answered_permission_prompt_returns_to_running(events):
    """Answering a prompt fires no event; the transcript moving on shows it."""
    asked = time.time()
    write_rows(events.transcript, PROMPT, _stamped(tool_use("Bash"), asked - 1))
    sessions = store.Sessions()
    events("UserPromptSubmit")
    events("Notification", message="Claude needs your permission to use Bash")
    sessions.poll()
    sessions.reconcile()
    assert sessions.aggregate() == "waiting"  # the tool_use predates the prompt
    result = {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}}
    write_rows(events.transcript, PROMPT, tool_use("Bash"), _stamped(result, asked + 5))
    sessions.reconcile()
    assert sessions.aggregate() == "running"


def test_turn_found_ended_is_dated_from_the_transcript(events):
    """So a tray restart does not give long-finished turns fresh green time."""
    sessions = store.Sessions()
    events("UserPromptSubmit", ts=time.time() - 3600)
    sessions.poll()
    finished = time.time() - 1800
    write_rows(events.transcript, PROMPT, _stamped(END_TURN, finished))
    sessions.reconcile()
    (sess,) = sessions.active()
    assert sess.status == "idle"
    assert abs(sess.since - finished) < 0.01


def test_a_just_opened_session_is_quiet_not_green(events):
    sessions = store.Sessions()
    events("SessionStart")
    sessions.poll()
    assert sessions.aggregate() == "quiet"


def test_a_session_rebuilt_from_history_keeps_its_age(events):
    events("SessionStart", ts=time.time() - 3600)
    sessions = store.Sessions()  # a tray starting later, replaying the log
    sessions.poll()
    (sess,) = sessions.active()
    assert time.time() - sess.since > 3500


def test_quiet_never_hides_work_in_progress(events, monkeypatch):
    sessions = store.Sessions()
    events("UserPromptSubmit", sid="A")
    events("Stop", sid="B")
    sessions.poll()
    later = time.time() + store.IDLE_FADE + 1
    monkeypatch.setattr(time, "time", lambda: later)
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
