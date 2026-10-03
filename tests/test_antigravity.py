"""Antigravity support: its hook payload, its hooks.json, and its turn events.

The payload and config shapes follow antigravity.google/docs/hooks -- Antigravity
was not installed when this was written, so these pin the documented format."""
import io
import json
import sys

import install
from agentwatch import hook, paths, store


def run_hook(monkeypatch, event, payload):
    monkeypatch.setattr(hook, "_spawn_tray", lambda: None)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(sys, "argv", ["hook", "--tool", "antigravity", "--event", event])
    hook.main()
    return json.loads(paths.EVENTS.read_text(encoding="utf-8").splitlines()[-1])


def test_hook_reads_antigravity_field_names(events, monkeypatch):
    payload = {
        "conversationId": "c-123",
        "workspacePaths": [r"C:\work\shop", r"C:\work\lib"],
        "transcriptPath": r"C:\x\transcript.jsonl",
        "modelName": "gemini-3-pro",
        "invocationNum": 0,
    }
    ev = run_hook(monkeypatch, "PreInvocation", payload)
    assert ev["tool"] == "antigravity"
    assert ev["session"] == "c-123"
    assert ev["cwd"] == r"C:\work\shop"
    assert ev["model"] == "gemini-3-pro"
    assert ev["transcript"] == r"C:\x\transcript.jsonl"


def test_antigravity_turn_runs_then_toasts_on_stop(events):
    sessions = store.Sessions()
    events("PreInvocation", sid="c-1", tool="antigravity")
    assert sessions.poll() == []
    assert sessions.aggregate() == "running"
    events("Stop", sid="c-1", tool="antigravity")
    assert [ev["event"] for ev, _ in sessions.poll()] == ["Stop"]
    assert sessions.aggregate() == "idle"


def _gemini(tmp_path, monkeypatch):
    home = tmp_path / ".gemini"
    home.mkdir()
    monkeypatch.setattr(paths, "GEMINI_HOME", home)
    return home / "config" / "hooks.json"


def test_install_adds_our_group_and_keeps_others(tmp_path, monkeypatch):
    target = _gemini(tmp_path, monkeypatch)
    target.parent.mkdir()
    theirs = {"my-linter": {"PostToolUse": [{"matcher": "*", "hooks": [{"command": "lint"}]}]}}
    target.write_text(json.dumps(theirs), encoding="utf-8")

    install.install_antigravity(remove=False)
    install.install_antigravity(remove=False)  # re-running replaces, never duplicates
    config = json.loads(target.read_text(encoding="utf-8"))
    assert config["my-linter"] == theirs["my-linter"]
    ours = config["agentwatch"]
    assert sorted(ours) == ["PreInvocation", "Stop"]
    command = ours["Stop"][0]["command"]
    assert "--tool antigravity --event Stop --source agentwatch" in command
    assert chr(92) not in command  # forward slashes only: same in cmd, PowerShell, bash

    install.install_antigravity(remove=True)
    assert json.loads(target.read_text(encoding="utf-8")) == theirs


def test_install_skips_when_antigravity_is_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "GEMINI_HOME", tmp_path / ".gemini")
    assert install.install_antigravity(remove=False).startswith("skipped")
    assert not (tmp_path / ".gemini").exists()
