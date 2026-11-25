import io
import json
import sys

import install
from agentwatch import hook, paths, proc, store, tray, winui

# A fake /proc: the hook's shell (100) under the CLI (99), which leads its own
# process group, as a CLI started from a terminal does.
PROC = {
    100: {"comm": "sh\n", "stat": "100 (sh) S 99 99 1 0"},
    99: {"comm": "claude\n", "stat": "99 (claude) S 50 99 50 0"},
    98: {"comm": "claude\n", "stat": "98 (claude) S 50 50 50 0"},
    97: {"comm": "vim\n", "stat": "97 (vim) S 50 97 50 0"},
}


def fake_proc(monkeypatch):
    monkeypatch.setattr(
        proc, "_wsl_read", lambda distro, pid, name: PROC.get(pid, {}).get(name, "")
    )
    monkeypatch.setattr(proc, "wsl_alive", lambda distro, pid: pid in PROC)
    killed = []
    monkeypatch.setattr(
        proc.subprocess,
        "run",
        lambda args, **kw: killed.append(args) or type("Done", (), {"returncode": 0}),
    )
    return killed


def test_wsl_owner_walks_past_the_hook_shell(monkeypatch):
    fake_proc(monkeypatch)
    assert proc.wsl_owner("Ubuntu", 100) == 99
    assert proc.wsl_owner("Ubuntu", 99) == 99


def test_wsl_end_ends_the_group_of_a_group_leader_only(monkeypatch):
    killed = fake_proc(monkeypatch)
    assert proc.wsl_end("Ubuntu", 99) is True
    assert proc.wsl_end("Ubuntu", 98) is True  # not a leader: the CLI alone
    assert [a[-1] for a in killed] == ["-99", "98"]
    assert killed[0][:3] == ["wsl.exe", "-d", "Ubuntu"]


def test_wsl_end_refuses_a_reused_pid(monkeypatch):
    killed = fake_proc(monkeypatch)
    assert proc.wsl_end("Ubuntu", 97) is False  # vim now holds the pid
    assert proc.wsl_end("Ubuntu", 12345) is False  # gone
    assert killed == []


def run_hook(monkeypatch, payload, *args):
    monkeypatch.setattr(
        sys, "argv", ["hook_entry.py", "--tool", "claude", "--event", "Stop", *args]
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    recorded = []
    monkeypatch.setattr(store, "append", recorded.append)
    hook.main()
    return recorded[0]


def test_hook_from_wsl_records_the_linux_pid_and_a_windows_transcript_path(monkeypatch):
    monkeypatch.setattr(proc, "wsl_owner", lambda distro, pid: pid - 1)
    payload = {
        "session_id": "S",
        "cwd": "/home/u/repo",
        "transcript_path": "/home/u/.claude/projects/p/S.jsonl",
    }
    event = run_hook(monkeypatch, payload, "--wsl", "Ubuntu", "--wsl-pid", "100")
    assert event["pid"] == 99
    assert event["wsl"] == "Ubuntu"
    assert (
        event["transcript"]
        == r"\\wsl.localhost\Ubuntu\home\u\.claude\projects\p\S.jsonl"
    )


def test_hook_from_wsl_without_a_pid_records_none(monkeypatch):
    """A CLI that ran the command without a shell leaves $PPID unexpanded."""
    event = run_hook(
        monkeypatch, {"session_id": "S"}, "--wsl", "Ubuntu", "--wsl-pid", "$PPID"
    )
    assert event["pid"] == 0


def test_a_wsl_session_lives_by_its_linux_pid(events, monkeypatch):
    alive = {"wsl": True}
    monkeypatch.setattr(proc, "wsl_alive", lambda distro, pid: alive["wsl"])
    monkeypatch.setattr(proc, "process_alive", lambda pid: False)  # no such Windows pid
    sessions = store.Sessions()
    events("Stop", pid=4242, wsl="Ubuntu")
    sessions.poll()
    sessions.reconcile()
    (sess,) = sessions.active()
    assert sess.win_pid == 0  # never used to look up a Windows window
    alive["wsl"] = False
    sessions.reconcile()
    assert sessions.active() == []


def test_panel_rows_say_which_sessions_run_in_wsl(events, monkeypatch):
    monkeypatch.setattr(proc, "wsl_alive", lambda distro, pid: True)
    t = tray.Tray()
    events("Stop", sid="win")
    events("Stop", sid="linux", pid=4242, wsl="Ubuntu")
    t.tick()
    assert {r["sess"].sid: r["where"] for r in t.view} == {"win": "", "linux": "WSL Ubuntu"}


def test_ending_a_wsl_session_goes_through_wsl(events, monkeypatch):
    t = tray.Tray()
    events("Stop", pid=4242, wsl="Ubuntu")
    t.sessions.poll()
    ended = []
    monkeypatch.setattr(
        proc, "wsl_end", lambda distro, pid: ended.append((distro, pid)) or True
    )
    monkeypatch.setattr(proc, "end_tree", lambda pid: ended.append(pid) or True)
    monkeypatch.setattr(winui, "confirm", lambda title, text: True)
    t._end(t.idle_sessions())
    assert ended == [("Ubuntu", 4242)]
    assert t.sessions.active() == []


def test_wsl_hook_command_quotes_paths_and_passes_the_cli_pid(monkeypatch):
    monkeypatch.setattr(
        install, "HOOK_ENTRY", paths.Path(r"C:\My Projects\hook_entry.py")
    )
    command = install.wsl_command(
        "/mnt/c/Program Files/Python/python.exe", "claude", "Stop", "Ubuntu"
    )
    assert command == (
        "'/mnt/c/Program Files/Python/python.exe' 'C:/My Projects/hook_entry.py' "
        "--tool claude --event Stop --source agentwatch --wsl Ubuntu --wsl-pid $PPID"
    )


def test_install_wsl_writes_into_each_distro_that_has_the_cli(tmp_path, monkeypatch):
    monkeypatch.setattr(install, "wsl_distros", lambda: ["Ubuntu"])
    answers = {"printenv": "/home/u", "wslpath": "/mnt/c/Python/python.exe"}
    monkeypatch.setattr(install, "wsl", lambda distro, *args: answers[args[0]])
    monkeypatch.setattr(
        paths, "wsl_path", lambda distro, path: str(tmp_path / distro / path.strip("/"))
    )
    claude = tmp_path / "Ubuntu" / "home" / "u" / ".claude"
    claude.mkdir(parents=True)

    lines = install.install_wsl(remove=False)
    assert lines[1] == "wsl Ubuntu codex -> skipped (codex not found)"
    hooks = json.loads((claude / "settings.json").read_text(encoding="utf-8"))["hooks"]
    command = hooks["Stop"][0]["hooks"][0]["command"]
    assert command.startswith("/mnt/c/Python/python.exe ")
    assert command.endswith(
        "--tool claude --event Stop --source agentwatch --wsl Ubuntu --wsl-pid $PPID"
    )

    install.install_wsl(remove=True)
    assert "hooks" not in json.loads(
        (claude / "settings.json").read_text(encoding="utf-8")
    )
