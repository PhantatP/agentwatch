import time

import pytest

from agentwatch import tray, winui
from conftest import PROMPT, tool_use, write_rows


@pytest.fixture
def quiet_tray(events, monkeypatch):
    """A Tray that records what it would announce instead of showing it."""
    t = tray.Tray()
    t.first_poll = False
    t.said = []
    monkeypatch.setattr(t, "announce", lambda ev, sess: t.said.append(ev["event"]))
    return t


def test_done_is_held_then_announced(events, quiet_tray, monkeypatch):
    events("UserPromptSubmit")
    events("Stop")
    quiet_tray.tick()
    assert quiet_tray.said == []
    monkeypatch.setattr(time, "time", lambda real=time.time: real() + tray.DONE_GRACE + 1)
    quiet_tray.tick()
    assert quiet_tray.said == ["Stop"]


def test_done_dropped_when_next_turn_starts(events, quiet_tray, monkeypatch):
    events("Stop")
    quiet_tray.tick()
    events("UserPromptSubmit")
    quiet_tray.tick()
    monkeypatch.setattr(time, "time", lambda real=time.time: real() + tray.DONE_GRACE + 1)
    quiet_tray.tick()
    assert quiet_tray.said == []


def test_permission_is_not_held(events, quiet_tray):
    events("PermissionRequest")
    quiet_tray.tick()
    assert quiet_tray.said == ["PermissionRequest"]


def _announce_with(monkeypatch, events, foreground):
    t = tray.Tray()
    shown, flashed = [], []
    monkeypatch.setattr(t.icon, "notify", lambda body, title: shown.append(title))
    monkeypatch.setattr(winui, "is_foreground", lambda pid: foreground)
    monkeypatch.setattr(winui, "flash_for_pid", lambda pid, **kw: flashed.append(pid))
    events("Stop")
    for event, sess in t.sessions.poll():
        t.announce(event, sess)
    return shown, flashed


def test_no_toast_when_session_window_is_in_front(events, monkeypatch):
    assert _announce_with(monkeypatch, events, foreground=True) == ([], [])


def test_toast_when_session_window_is_behind(events, monkeypatch):
    shown, flashed = _announce_with(monkeypatch, events, foreground=False)
    assert shown == ["Claude is done"]
    assert len(flashed) == 1


def test_menu_shows_activity_and_named_agents(events):
    write_rows(events.transcript, PROMPT, tool_use("Edit", file_path=r"C:\x\tray.py"))
    agent = write_rows(events.agents / "agent-a.jsonl", PROMPT, tool_use("Bash", description="Run tests & lint"))
    agent.with_name("agent-a.meta.json").write_text(
        '{"agentType": "reviewer", "description": "Check the fix"}', encoding="utf-8"
    )
    t = tray.Tray()
    events("UserPromptSubmit")
    t.sessions.poll()
    t.sessions.reconcile()
    rows = [str(item.text) for item in t.build_menu()]
    assert any("running" in r and "1 agent" in r for r in rows)
    assert "        Edit tray.py" in rows
    assert "        ↳ reviewer · Check the fix — Bash Run tests && lint" in rows


def test_icon_marks_agents_with_a_centre_dot():
    centre = (32, 32)
    assert tray.make_icon("running", 1).getpixel(centre)[3] == 0
    assert tray.make_icon("running", 1, agents=2).getpixel(centre)[3] == 255


def test_fit_trims_on_a_word_boundary():
    assert tray._fit("short", 10) == "short"
    assert tray._fit("one two three four", 16) == "one two three…"
    # a space too early to cut at (before 60% of the limit) is not used
    assert tray._fit("one two three four", 12) == "one two thr…"
