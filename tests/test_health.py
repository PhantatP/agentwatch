import os
import time

from agentwatch import health, paths


def _fmt(ts):
    return "never" if not ts else "%dm ago" % ((time.time() - ts) // 60)


def _setup(tmp_path, monkeypatch, used_at, installed=True):
    """A Codex home with a transcript last written at `used_at`."""
    for name in ("CLAUDE_HOME", "GEMINI_HOME"):
        monkeypatch.setattr(paths, name, tmp_path / name)
    monkeypatch.setattr(paths, "CLAUDE_PROJECTS", tmp_path / "CLAUDE_HOME" / "projects")
    monkeypatch.setattr(paths, "CODEX_HOME", tmp_path / "codex")
    monkeypatch.setattr(paths, "CODEX_SESSIONS", tmp_path / "codex" / "sessions")
    day = tmp_path / "codex" / "sessions" / "2026" / "10" / "03"
    day.mkdir(parents=True)
    rollout = day / "rollout.jsonl"
    rollout.write_text("{}", encoding="utf-8")
    os.utime(rollout, (used_at, used_at))
    if installed:
        (tmp_path / "codex" / "hooks.json").write_text('{"hooks": "... --source agentwatch"}', encoding="utf-8")


def test_hooks_that_never_fire_are_flagged(tmp_path, monkeypatch):
    now = time.time()
    _setup(tmp_path, monkeypatch, used_at=now - 60)
    (row,) = health.check({"codex": now - 15 * 86400}, _fmt)
    tool, status, healthy = row
    assert (tool, healthy) == ("codex", False)
    assert "not firing" in status and "codex /hooks" in status


def test_working_hooks_show_their_last_event(tmp_path, monkeypatch):
    now = time.time()
    _setup(tmp_path, monkeypatch, used_at=now - 60)
    assert health.check({"codex": now - 120}, _fmt) == [("codex", "last event 2m ago", True)]


def test_a_long_turn_is_not_mistaken_for_silence(tmp_path, monkeypatch):
    now = time.time()
    _setup(tmp_path, monkeypatch, used_at=now)
    started = now - 40 * 60  # one 40-minute turn: its hook fired at the start
    assert health.check({"codex": started}, _fmt)[0][2] is True


def test_used_but_not_installed(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, used_at=time.time() - 60, installed=False)
    assert health.check({}, _fmt) == [("codex", "hooks not installed · run install.py", False)]


def test_a_long_unused_tool_without_hooks_is_left_out(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, used_at=time.time() - 60 * 86400, installed=False)
    assert health.check({}, _fmt) == []


def test_log_errors_counts_lines(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "LOG", tmp_path / "agentwatch.log")
    assert health.log_errors() == 0
    (tmp_path / "agentwatch.log").write_text("hook error: x\nhook error: y\n", encoding="utf-8")
    assert health.log_errors() == 2

