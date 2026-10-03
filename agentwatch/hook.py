"""Hook entrypoint for both Claude Code and Codex.

Both CLIs pass a JSON object on stdin containing `hook_event_name` and
`session_id`, so one script serves both; `--tool` says which one called.

This runs inline in the agent's turn, so it does the least possible work:
append one line, optionally spawn the tray, exit. It never fails loudly --
a crashing hook would surface as noise in the user's session.
"""
import json
import os
import subprocess
import sys

from . import paths, proc, store

# Claude Code runs a hook through a shell that exits with it, so the direct
# parent is gone by the time the tray checks it. Skip past to the CLI itself.
SHELLS = {"bash.exe", "sh.exe", "cmd.exe", "pwsh.exe", "powershell.exe"}


def _owner_pid() -> int:
    parent = os.getppid()
    for pid in [parent] + proc.parent_chain(parent):
        if proc.process_name(pid).lower() not in SHELLS:
            return pid
    return parent


def _spawn_tray() -> None:
    """Start the tray if it is not already running."""
    try:
        pid = int(paths.TRAY_LOCK.read_text().strip())
        if proc.process_alive(pid):
            return
    except (OSError, ValueError):
        pass
    exe = sys.executable
    pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pythonw):
        exe = pythonw
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    DETACHED_PROCESS, CREATE_NO_WINDOW = 0x00000008, 0x08000000
    subprocess.Popen(
        [exe, "-m", "agentwatch.tray"],
        cwd=root,
        creationflags=DETACHED_PROCESS | CREATE_NO_WINDOW,
        close_fds=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def main() -> int:
    tool = "claude"
    argv = sys.argv[1:]
    if "--tool" in argv:
        tool = argv[argv.index("--tool") + 1]

    raw = ""
    try:
        if not sys.stdin.isatty():
            raw = sys.stdin.read()
    except (OSError, ValueError):
        raw = ""
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    event = payload.get("hook_event_name") or payload.get("hookEventName") or ""
    if "--event" in argv:  # lets a config pin the event name explicitly
        event = argv[argv.index("--event") + 1]

    # Antigravity names these differently: conversationId, workspacePaths, modelName.
    workspaces = payload.get("workspacePaths")
    workspace = workspaces[0] if isinstance(workspaces, list) and workspaces else ""
    store.append(
        {
            "tool": tool,
            "session": payload.get("session_id")
            or payload.get("sessionId")
            or payload.get("conversationId")
            or "unknown",
            "event": event,
            "cwd": payload.get("cwd") or workspace or os.getcwd(),
            "pid": _owner_pid(),
            "model": payload.get("model") or payload.get("modelName") or "",
            "message": payload.get("message") or "",
            "transcript": payload.get("transcript_path") or payload.get("transcriptPath") or "",
        }
    )

    if event in ("SessionStart", "UserPromptSubmit", "PreInvocation"):
        _spawn_tray()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # never break the caller's turn
        try:
            paths.ensure_data()
            with open(paths.LOG, "a", encoding="utf-8") as fh:
                fh.write("hook error: %r" % (exc,) + chr(10))
        except OSError:
            pass
        sys.exit(0)
