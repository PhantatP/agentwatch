import json

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
