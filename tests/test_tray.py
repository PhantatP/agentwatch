import time

import pytest

from agentwatch import tray, winui
from conftest import PROMPT, stamped, tool_use, write_rows


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
    monkeypatch.setattr(winui, "is_foreground", lambda pid, cwd: foreground)
    monkeypatch.setattr(winui, "flash_for_pid", lambda pid, cwd, **kw: flashed.append(pid))
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


@pytest.fixture
def watched(events, monkeypatch):
    """A Tray whose toasts are recorded, with the session window in front or
    not as the test says, and a clock the test can move forward."""
    t = tray.Tray()
    t.toasts, t.in_front, t.clock = [], True, [time.time()]
    monkeypatch.setattr(t.icon, "notify", lambda body, title: t.toasts.append(title))
    monkeypatch.setattr(winui, "is_foreground", lambda pid, cwd: t.in_front)
    monkeypatch.setattr(winui, "flash_for_pid", lambda pid, cwd, **kw: None)
    monkeypatch.setattr(time, "time", lambda: t.clock[0])

    def later(seconds):
        t.clock[0] += seconds
        t.tick()

    t.later = later
    t.settings["toasts"] = True
    return t


def test_unseen_finish_in_front_is_reminded_after_3_minutes(events, watched):
    events("UserPromptSubmit", ts=watched.clock[0])
    events("Stop", ts=watched.clock[0])
    watched.later(tray.DONE_GRACE + 1)
    assert watched.toasts == []  # its window is in front: no toast yet
    watched.later(tray.REMIND_AFTER["done"])
    assert watched.toasts == ["Claude is still waiting on you"]
    watched.later(600)
    assert len(watched.toasts) == 1  # once per wait


def test_finish_already_toasted_is_not_reminded(events, watched):
    watched.in_front = False
    events("UserPromptSubmit", ts=watched.clock[0])
    events("Stop", ts=watched.clock[0])
    watched.later(tray.DONE_GRACE + 1)
    watched.later(tray.REMIND_AFTER["done"] + 60)
    assert watched.toasts == ["Claude is done"]


def test_unanswered_permission_is_reminded_after_1_minute(events, watched):
    watched.in_front = False  # Claude's permission prompts never toast at all
    asking = stamped(tool_use("Bash"), watched.clock[0] - 1)  # written just before it asked
    write_rows(events.transcript, PROMPT, asking)
    events("UserPromptSubmit", ts=watched.clock[0])
    events("Notification", ts=watched.clock[0], message="Claude needs your permission to use Bash")
    watched.later(30)
    assert watched.toasts == []
    watched.later(tray.REMIND_AFTER["permission"])
    assert watched.toasts == ["Claude still needs your go-ahead"]


def test_a_reply_in_time_cancels_the_reminder(events, watched):
    events("UserPromptSubmit", ts=watched.clock[0])
    events("Stop", ts=watched.clock[0])
    watched.later(60)
    events("UserPromptSubmit", ts=watched.clock[0])
    watched.later(tray.REMIND_AFTER["done"] + 60)
    assert watched.toasts == []


def test_an_interrupt_is_not_reminded(events, watched):
    events("UserPromptSubmit", sid="X", tool="codex", ts=watched.clock[0])
    events("Interrupt", sid="X", tool="codex", ts=watched.clock[0])
    watched.later(tray.REMIND_AFTER["done"] + 60)
    assert watched.toasts == []


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


def _on_arc(image, degrees):
    """The pixel mid-stroke on the arc at `degrees` (clockwise from 3 o'clock)."""
    import math

    box_edge = 64 * 0.09
    far = 64 - box_edge - 64 * 0.17  # the glyph shrinks when a badge is shown
    centre = (box_edge + far) / 2
    radius = (far - box_edge) / 2 - 64 * 0.17 / 2
    x = centre + radius * math.cos(math.radians(degrees))
    y = centre + radius * math.sin(math.radians(degrees))
    return image.getpixel((round(x), round(y)))[:3]


def test_running_arc_takes_each_tools_colour():
    icon = tray.make_icon("running", 2, tools=["claude", "codex"])
    assert _on_arc(icon, 180) == tray.TOOL_COLORS["claude"]
    assert _on_arc(icon, 300) == tray.TOOL_COLORS["codex"]


def test_codex_turns_black_on_a_light_taskbar():
    dark = tray.make_icon("running", 1, tools=["codex"])
    light = tray.make_icon("running", 1, tools=["codex"], light=True)
    assert _on_arc_single(dark) == tray.TOOL_COLORS["codex"]
    assert _on_arc_single(light) == tray.LIGHT_TASKBAR_COLORS["codex"]


def _on_arc_single(image):
    """Mid-stroke on the left of the arc, for an icon with no badge."""
    edge, stroke = 64 * 0.09, 64 * 0.17
    return image.getpixel((round(edge + stroke / 2), 32))[:3]


def test_limit_warning_fires_once_per_window(monkeypatch):
    t = tray.Tray()
    shown = []
    monkeypatch.setattr(t.icon, "notify", lambda body, title: shown.append(title))
    resets = time.time() + 3600
    t.claude_limits.windows = [
        {"label": "5h block", "percent": 93.0, "resets_at": resets},
        {"label": "week", "percent": 40.0, "resets_at": resets + 86400},
    ]
    t.warn_limits()
    t.warn_limits()
    assert shown == ["Claude 5h block limit at 93%"]
    t.claude_limits.windows[0]["resets_at"] = resets + 5 * 3600  # the next window
    t.warn_limits()
    assert len(shown) == 2


def test_tick_colours_the_arc_by_running_tool(events, monkeypatch):
    seen = {}
    monkeypatch.setattr(tray, "make_icon", lambda *a, **kw: seen.update(kw) or tray.Image.new("RGBA", (1, 1)))
    t = tray.Tray()
    events("UserPromptSubmit", sid="A", tool="codex")
    events("UserPromptSubmit", sid="B", tool="claude")
    t.tick()
    assert seen["tools"] == ["claude", "codex"]  # fixed order, so colours do not jump


def test_fit_trims_on_a_word_boundary():
    assert tray._fit("short", 10) == "short"
    assert tray._fit("one two three four", 16) == "one two three…"
    # a space too early to cut at (before 60% of the limit) is not used
    assert tray._fit("one two three four", 12) == "one two thr…"
