"""Wire agentwatch into Claude Code and Codex.

Idempotent: every entry it adds carries an `agentwatch` marker, so re-running
replaces its own entries and leaves any other hooks untouched. Both config
files are backed up before the first change.

    python install.py            # install into both CLIs
    python install.py --uninstall
"""
import json
import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from agentwatch import paths  # noqa: E402

ROOT = Path(os.path.dirname(os.path.abspath(__file__)))
HOOK_ENTRY = ROOT / "hook_entry.py"
MARKER = "agentwatch"

# Claude Code fires Notification for permission prompts and idle waits; there is
# no distinct error event, so Stop covers both success and failure endings.
CLAUDE_EVENTS = ["SessionStart", "UserPromptSubmit", "Notification", "Stop", "SessionEnd"]

# Codex adds PermissionRequest and Interrupt, which are sharper than Claude's.
CODEX_EVENTS = [
    "SessionStart",
    "UserPromptSubmit",
    "PermissionRequest",
    "Stop",
    "Interrupt",
    "SessionEnd",
]

# Antigravity has no prompt-submit event: PreInvocation fires before every model
# call, so it marks the turn as working, and Stop fires when the run ends.
ANTIGRAVITY_EVENTS = ["PreInvocation", "Stop"]


def python_exe() -> str:
    return sys.executable


def backup(path: Path) -> None:
    if path.exists():
        stamp = time.strftime("%Y%m%d%H%M%S")
        shutil.copy2(path, path.with_name(path.name + ".agentwatch-backup-" + stamp))


def load_json(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write(chr(10))
    os.replace(tmp, path)


def is_ours(entry: dict) -> bool:
    return MARKER in json.dumps(entry)


def strip_ours(hooks: dict) -> dict:
    """Drop our matcher groups, and any event key we emptied."""
    cleaned = {}
    for event, groups in (hooks or {}).items():
        if not isinstance(groups, list):
            cleaned[event] = groups
            continue
        kept = []
        for group in groups:
            inner = group.get("hooks") if isinstance(group, dict) else None
            if isinstance(inner, list):
                survivors = [h for h in inner if not is_ours(h)]
                if not survivors:
                    continue
                group = dict(group, hooks=survivors)
            kept.append(group)
        if kept:
            cleaned[event] = kept
    return cleaned


def claude_group(event: str) -> dict:
    command = '"%s" "%s" --tool claude --event %s --source %s' % (
        python_exe(), HOOK_ENTRY, event, MARKER)
    return {"hooks": [{"type": "command", "command": command, "timeout": 10}]}


def codex_group(event: str) -> dict:
    # Codex wants `command` as a single string, not a list -- verified against
    # the app-server `hooks/list` RPC, which rejects a sequence outright.
    command = '"%s" "%s" --tool codex --event %s --source %s' % (
        python_exe(), HOOK_ENTRY, event, MARKER)
    # Codex caps teardown hooks at 3s and always runs SessionEnd synchronously;
    # match that here so it has nothing to clamp or warn about.
    teardown = event in ("SessionEnd", "Interrupt")
    return {
        "hooks": [
            {
                "type": "command",
                "command": command,
                "timeout": 3 if teardown else 10,
                "async": event != "SessionEnd",
            }
        ]
    }


def install_claude(remove: bool) -> str:
    target = paths.CLAUDE_HOME / "settings.json"
    settings = load_json(target)
    backup(target)
    hooks = strip_ours(settings.get("hooks") or {})
    if not remove:
        for event in CLAUDE_EVENTS:
            hooks.setdefault(event, []).append(claude_group(event))
    if hooks:
        settings["hooks"] = hooks
    else:
        settings.pop("hooks", None)
    write_json(target, settings)
    return str(target)


def install_codex(remove: bool) -> str:
    # Codex reads ~/.codex/hooks.json. It does NOT read ~/.codex/hooks/hooks.json,
    # which is a plugin-only path -- files there are ignored without any warning.
    stray = paths.CODEX_HOME / "hooks" / "hooks.json"
    if stray.exists():
        try:
            stray.unlink()
            stray.parent.rmdir()
        except OSError:
            pass
    target = paths.CODEX_HOME / "hooks.json"
    config = load_json(target)
    backup(target)
    hooks = strip_ours(config.get("hooks") or {})
    if not remove:
        for event in CODEX_EVENTS:
            hooks.setdefault(event, []).append(codex_group(event))
    config["hooks"] = hooks
    write_json(target, config)
    return str(target)


def antigravity_command(event: str) -> str:
    # Which shell Antigravity runs hooks in on Windows is not documented. Forward
    # slashes and no quotes (unless a path has spaces) read the same in cmd,
    # PowerShell, and bash; a quoted path is not a command in PowerShell.
    def arg(path) -> str:
        text = str(path).replace(chr(92), "/")
        return '"%s"' % text if " " in text else text

    return "%s %s --tool antigravity --event %s --source %s" % (
        arg(python_exe()), arg(HOOK_ENTRY), event, MARKER)


def install_antigravity(remove: bool) -> str:
    # One hooks.json, shared by the Antigravity IDE, CLI, and app. Its top level
    # is named groups, so ours is the "agentwatch" group and nothing else is touched.
    if not paths.GEMINI_HOME.exists():
        return "skipped (Antigravity not found)"
    target = paths.GEMINI_HOME / "config" / "hooks.json"
    config = load_json(target)
    backup(target)
    config.pop(MARKER, None)
    if not remove:
        config[MARKER] = {
            event: [{"type": "command", "command": antigravity_command(event), "timeout": 10}]
            for event in ANTIGRAVITY_EVENTS
        }
    write_json(target, config)
    return str(target)


def main(argv) -> int:
    remove = "--uninstall" in argv
    if not HOOK_ENTRY.exists():
        print("cannot find " + str(HOOK_ENTRY))
        return 1
    paths.ensure_data()
    print(("Removing" if remove else "Installing") + " agentwatch hooks")
    print("  claude -> " + install_claude(remove))
    print("  codex  -> " + install_codex(remove))
    print("  antigravity -> " + install_antigravity(remove))
    print("  state  -> " + str(paths.DATA))
    if not remove:
        print(chr(10) + "The tray starts itself on the next agent turn.")
        print('Start it now with:  "%s" -m agentwatch.tray' % python_exe())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
