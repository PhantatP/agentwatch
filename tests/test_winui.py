"""Finding a session's own window when one process owns several -- every VS
Code window belongs to the same Code.exe."""
from agentwatch import winui

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
