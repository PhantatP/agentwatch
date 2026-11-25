"""Wire agentwatch into Claude Code and Codex, on Windows and in WSL.

Idempotent: every entry it adds carries an `agentwatch` marker, so re-running
replaces its own entries and leaves any other hooks untouched. Both config
files are backed up before the first change.

    python install.py            # install into both CLIs
    python install.py --uninstall
"""
import json
import os
import shlex
import shutil
import subprocess
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


def claude_group(event: str, command: str = "") -> dict:
    command = command or '"%s" "%s" --tool claude --event %s --source %s' % (
        python_exe(), HOOK_ENTRY, event, MARKER)
    return {"hooks": [{"type": "command", "command": command, "timeout": 10}]}


def portable_command(tool: str, event: str) -> str:
    """A hook command that runs the same in PowerShell, cmd, and bash.

    Codex runs hooks through PowerShell on Windows, where a line that starts
    with a quoted path is a parse error, not a command -- so the hook never ran.
    Forward slashes and no quotes avoid that; a path with a space still needs
    quotes, and would break PowerShell again.
    """
    def arg(path) -> str:
        text = str(path).replace(chr(92), "/")
        return '"%s"' % text if " " in text else text

    return "%s %s --tool %s --event %s --source %s" % (
        arg(python_exe()), arg(HOOK_ENTRY), tool, event, MARKER)


def wsl_command(python: str, tool: str, event: str, distro: str) -> str:
    """A hook command for a CLI inside WSL. It runs Windows Python through WSL
    interop, so the event lands in the same log, under the same lock, as one
    from Windows. The shell running the hook expands $PPID: the CLI's pid."""
    return "%s %s --tool %s --event %s --source %s --wsl %s --wsl-pid $PPID" % (
        shlex.quote(python), shlex.quote(str(HOOK_ENTRY).replace(chr(92), "/")),
        tool, event, MARKER, shlex.quote(distro))


def codex_group(event: str, command: str = "") -> dict:
    # Codex wants `command` as a single string, not a list -- verified against
    # the app-server `hooks/list` RPC, which rejects a sequence outright.
    command = command or portable_command("codex", event)
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


def install_claude(remove: bool, home: Path = None, command=None) -> str:
    """`home` and `command(event)` default to Claude Code on Windows."""
    target = (home or paths.CLAUDE_HOME) / "settings.json"
    settings = load_json(target)
    backup(target)
    hooks = strip_ours(settings.get("hooks") or {})
    if not remove:
        for event in CLAUDE_EVENTS:
            hooks.setdefault(event, []).append(claude_group(event, command(event) if command else ""))
    if hooks:
        settings["hooks"] = hooks
    else:
        settings.pop("hooks", None)
    write_json(target, settings)
    return str(target)


def drop_stray(stray: Path) -> None:
    """Take our entries out of a hooks file an old install wrote where Codex
    never looks. Anyone else's entries stay, so the file goes only when
    nothing else is left in it, and is backed up before any rewrite."""
    config = load_json(stray)
    hooks = config.get("hooks")
    if not isinstance(hooks, dict):
        return
    kept = strip_ours(hooks)
    if kept == hooks:
        return  # nothing of ours in it
    if kept or set(config) != {"hooks"}:
        backup(stray)
        config["hooks"] = kept
        write_json(stray, config)
        return
    try:
        stray.unlink()
        stray.parent.rmdir()
    except OSError:
        pass


def install_codex(remove: bool, home: Path = None, command=None) -> str:
    """`home` and `command(event)` default to Codex on Windows."""
    home = home or paths.CODEX_HOME
    # Codex reads ~/.codex/hooks.json. It does NOT read ~/.codex/hooks/hooks.json,
    # which is a plugin-only path -- files there are ignored without any warning.
    drop_stray(home / "hooks" / "hooks.json")
    target = home / "hooks.json"
    config = load_json(target)
    backup(target)
    hooks = strip_ours(config.get("hooks") or {})
    if not remove:
        for event in CODEX_EVENTS:
            hooks.setdefault(event, []).append(codex_group(event, command(event) if command else ""))
    config["hooks"] = hooks
    write_json(target, config)
    return str(target)


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
            event: [{"type": "command", "command": portable_command("antigravity", event), "timeout": 10}]
            for event in ANTIGRAVITY_EVENTS
        }
    write_json(target, config)
    return str(target)


def wsl(distro: str, *args) -> str:
    """Run a program in a WSL distro: its output, or '' if it failed."""
    try:
        done = subprocess.run(["wsl.exe", "-d", distro, "-e", *args], capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return done.stdout.decode("utf-8", "replace").strip() if done.returncode == 0 else ""


def wsl_distros() -> list:
    """Installed WSL distros, less Docker Desktop's own."""
    try:
        done = subprocess.run(["wsl.exe", "--list", "--quiet"], capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if done.returncode != 0:
        return []
    raw = done.stdout  # UTF-16, unless WSL_UTF8 is set
    text = raw.decode("utf-16-le" if bytes(1) in raw else "utf-8", "replace")
    names = [line.strip().lstrip(chr(0xFEFF)) for line in text.splitlines()]
    return [name for name in names if name and not name.startswith("docker-desktop")]


def install_wsl(remove: bool) -> list:
    """Hooks for Claude Code and Codex in each WSL distro that has them. Each
    distro has its own home, so its CLIs never read the Windows config."""
    lines = []
    for distro in wsl_distros():
        home = wsl(distro, "printenv", "HOME")
        python = wsl(distro, "wslpath", "-u", python_exe())
        if not home.startswith("/") or not python:
            lines.append("wsl %s -> skipped (could not read it)" % distro)
            continue
        root = Path(paths.wsl_path(distro, home))
        for tool, install in (("claude", install_claude), ("codex", install_codex)):
            label = "wsl %s %s -> " % (distro, tool)
            if not (root / ("." + tool)).is_dir():
                lines.append(label + "skipped (%s not found)" % tool)
                continue
            command = lambda event, tool=tool: wsl_command(python, tool, event, distro)  # noqa: E731
            lines.append(label + install(remove, root / ("." + tool), command))
    return lines


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
    for line in install_wsl(remove):
        print("  " + line)
    print("  state  -> " + str(paths.DATA))
    if not remove:
        print(chr(10) + "The tray starts itself on the next agent turn.")
        print('Start it now with:  "%s" -m agentwatch.tray' % python_exe())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
