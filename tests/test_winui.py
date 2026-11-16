"""Finding a session's own window when one process owns several -- every VS
Code window belongs to the same Code.exe."""
from agentwatch import winui
from agentwatch.winui import toast as real_toast  # conftest stubs winui.toast

TITLES = {
    1: "README.md - indicator - Visual Studio Code",
    2: "classifier.py - indy - Visual Studio Code",
    3: "● tray.py - indicator-old - Visual Studio Code",
}


def _pick(monkeypatch, hwnds, cwd):
    monkeypatch.setattr(winui, "_title", lambda hwnd: TITLES[hwnd])
    return winui._by_title(hwnds, cwd)


def test_picks_the_window_titled_for_the_session_folder(monkeypatch):
    assert _pick(monkeypatch, [1, 2, 3], r"C:\Users\phant\OneDrive\indy") == 2
    assert _pick(monkeypatch, [2, 3, 1], r"C:\Users\phant\OneDrive\indicator") == 1


def test_a_subfolder_session_matches_its_workspace(monkeypatch):
    assert _pick(monkeypatch, [1, 2], r"C:\Users\phant\OneDrive\indy\src\models") == 2


def test_no_match_among_several_is_unknown(monkeypatch):
    assert _pick(monkeypatch, [1, 2], r"C:\work\shop") is None


def test_a_lone_window_needs_no_title(monkeypatch):
    assert _pick(monkeypatch, [2], r"C:\work\shop") == 2


def test_another_window_of_the_same_app_is_not_foreground(monkeypatch):
    monkeypatch.setattr(winui, "session_window", lambda pid, cwd="", fallback=False: 1)
    monkeypatch.setattr(winui._user32, "GetForegroundWindow", lambda: 2)
    assert winui.is_foreground(42, r"C:\x\indicator") is False
    monkeypatch.setattr(winui._user32, "GetForegroundWindow", lambda: 1)
    assert winui.is_foreground(42, r"C:\x\indicator") is True


def test_toast_text_never_enters_the_command(monkeypatch):
    import base64
    import json

    class FakeProcess:
        def __init__(self, args, **kw):
            self.args, self.stdin, self.written = args, self, []

        def poll(self):
            return None

        def write(self, data):
            self.written.append(data)

        def flush(self):
            pass

    started = []
    monkeypatch.setattr(winui.subprocess, "Popen", lambda args, **kw: started.append(FakeProcess(args)) or started[-1])
    monkeypatch.setattr(winui, "_toaster", None)
    real_toast('Claude is done"; Remove-Item x', "Wrapped up in <indy> & co")
    real_toast("second", "reuses the same process")
    (proc,) = started  # one PowerShell for both
    assert proc.args[:2] == ["powershell.exe", "-NoProfile"]
    assert "Remove-Item" not in " ".join(proc.args)
    sent = [json.loads(base64.b64decode(line)) for line in proc.written]
    assert sent[0] == {"title": 'Claude is done"; Remove-Item x', "body": "Wrapped up in <indy> & co"}
    assert sent[1]["title"] == "second"
