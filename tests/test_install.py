import json
import os

import install
from agentwatch import paths


def test_codex_hooks_use_a_powershell_safe_command(tmp_path, monkeypatch):
    """Codex runs hooks through PowerShell, where a line starting with a quoted
    path is a parse error -- the hook silently never runs."""
    monkeypatch.setattr(paths, "CODEX_HOME", tmp_path)
    target = tmp_path / "hooks.json"
    theirs = {"Stop": [{"hooks": [{"type": "command", "command": "notify-me"}]}]}
    target.write_text(json.dumps({"hooks": theirs}), encoding="utf-8")

    install.install_codex(remove=False)
    install.install_codex(remove=False)  # re-running replaces, never duplicates
    hooks = json.loads(target.read_text(encoding="utf-8"))["hooks"]
    assert hooks["Stop"][0] == theirs["Stop"][0]
    assert len(hooks["Stop"]) == 2
    command = hooks["Stop"][1]["hooks"][0]["command"]
    assert not command.startswith('"')
    assert chr(92) not in command
    assert command.endswith("--tool codex --event Stop --source agentwatch")

    install.install_codex(remove=True)
    assert json.loads(target.read_text(encoding="utf-8"))["hooks"] == theirs


OURS = {"type": "command", "command": "python hook_entry.py --source agentwatch"}
THEIRS = {"type": "command", "command": "notify-me"}


def write_stray(home, hooks):
    stray = home / "hooks" / "hooks.json"
    stray.parent.mkdir(parents=True)
    stray.write_text(json.dumps({"hooks": hooks}), encoding="utf-8")
    return stray


def test_stray_codex_file_keeps_other_hooks(tmp_path):
    stray = write_stray(tmp_path, {"Stop": [{"hooks": [OURS, THEIRS]}]})
    install.drop_stray(stray)
    assert json.loads(stray.read_text(encoding="utf-8"))["hooks"] == {
        "Stop": [{"hooks": [THEIRS]}]
    }
    assert any(
        p.name.startswith("hooks.json.agentwatch-backup-")
        for p in stray.parent.iterdir()
    )


def test_stray_codex_file_of_only_ours_is_removed(tmp_path):
    stray = write_stray(tmp_path, {"Stop": [{"hooks": [OURS]}]})
    install.drop_stray(stray)
    assert not stray.parent.exists()


def test_stray_codex_file_without_ours_is_left_alone(tmp_path):
    stray = write_stray(tmp_path, {"Stop": [{"hooks": [THEIRS]}]})
    before = stray.read_bytes()
    install.drop_stray(stray)
    assert stray.read_bytes() == before
    assert os.listdir(stray.parent) == ["hooks.json"]
