from agentwatch import hook, proc

NAMES = {10: "bash.exe", 11: "bash.exe", 12: "claude.exe", 13: "Code.exe"}


def test_owner_pid_skips_the_hook_shell(monkeypatch):
    monkeypatch.setattr(hook.os, "getppid", lambda: 10)
    monkeypatch.setattr(proc, "parent_chain", lambda pid: [11, 12, 13])
    monkeypatch.setattr(proc, "process_name", lambda pid: NAMES.get(pid, ""))
    assert hook._owner_pid() == 12


def test_owner_pid_when_the_cli_runs_the_hook_directly(monkeypatch):
    monkeypatch.setattr(hook.os, "getppid", lambda: 12)
    monkeypatch.setattr(proc, "parent_chain", lambda pid: [13])
    monkeypatch.setattr(proc, "process_name", lambda pid: NAMES.get(pid, ""))
    assert hook._owner_pid() == 12
