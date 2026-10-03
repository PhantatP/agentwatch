"""Taskbar flashing for the terminal window that owns a session.

A hook records its parent pid, which is the agent CLI. The window we want to
flash belongs to whatever terminal hosts it, so we walk up the process tree
and flash the first ancestor that owns a visible top-level window.
"""
import ctypes
import re
import winreg
from ctypes import wintypes

from . import proc

_user32 = ctypes.WinDLL("user32", use_last_error=True)

FLASHW_STOP = 0
FLASHW_ALL = 0x00000003
FLASHW_TIMERNOFG = 0x0000000C
GW_OWNER = 4


class _FLASHWINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("hwnd", wintypes.HWND),
        ("dwFlags", wintypes.DWORD),
        ("uCount", wintypes.UINT),
        ("dwTimeout", wintypes.DWORD),
    ]


_ENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def _windows_by_pid() -> dict:
    """pid -> [hwnd, ...] for visible, unowned top-level windows with a title."""
    found = {}

    def callback(hwnd, _lparam):
        if not _user32.IsWindowVisible(hwnd):
            return True
        if _user32.GetWindow(hwnd, GW_OWNER):
            return True
        if not _user32.GetWindowTextLengthW(hwnd):
            return True
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        found.setdefault(pid.value, []).append(hwnd)
        return True

    _user32.EnumWindows(_ENUMPROC(callback), 0)
    return found


def _title(hwnd) -> str:
    length = _user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _by_title(hwnds: list, cwd: str):
    """Pick the session's window among several owned by one process.

    Every VS Code window belongs to the same Code.exe, so the process alone
    cannot say which window is the session's. The title can: VS Code puts the
    workspace folder in it ("tray.py - indicator - Visual Studio Code"). Match
    the session's folder, or failing that its nearest parent (a session started
    in a subfolder). None when no title says.
    """
    if len(hwnds) == 1:
        return hwnds[0]
    parts = [p.lower() for p in re.split(r"[\\/]+", cwd or "") if p][::-1][:4]
    titles = {hwnd: [t.strip().lower() for t in _title(hwnd).split(" - ")] for hwnd in hwnds}
    for part in parts:
        for hwnd in hwnds:
            if part in titles[hwnd]:
                return hwnd
    return None


def session_window(pid: int, cwd: str = "", fallback: bool = False):
    """The window hosting the session whose agent CLI is `pid`: the first
    ancestor owning windows, then the one of those titled for `cwd`. With
    `fallback`, an ambiguous match settles for that ancestor's first window."""
    if not pid:
        return None
    windows = _windows_by_pid()
    for candidate in [pid] + proc.parent_chain(pid):
        if candidate in windows:
            hwnd = _by_title(windows[candidate], cwd)
            return hwnd or (windows[candidate][0] if fallback else None)
    return None


def light_taskbar() -> bool:
    """Whether the taskbar, and so the tray, uses the light theme."""
    key = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as handle:
            return winreg.QueryValueEx(handle, "SystemUsesLightTheme")[0] == 1
    except OSError:
        return False  # older Windows: the taskbar is dark


def is_foreground(pid: int, cwd: str = "") -> bool:
    """Whether the session's own window is in front -- you are looking at it.
    Another window of the same app (a second VS Code window) does not count,
    and when the windows cannot be told apart the answer is no, so you are
    notified rather than missed."""
    hwnd = session_window(pid, cwd)
    return hwnd is not None and hwnd == _user32.GetForegroundWindow()


def flash_for_pid(pid: int, cwd: str = "", count: int = 4) -> bool:
    """Flash the window hosting the session. Returns whether one was found."""
    hwnd = session_window(pid, cwd, fallback=True)
    if not hwnd:
        return False
    info = _FLASHWINFO(
        cbSize=ctypes.sizeof(_FLASHWINFO),
        hwnd=hwnd,
        dwFlags=FLASHW_ALL | FLASHW_TIMERNOFG,
        uCount=count,
        dwTimeout=0,
    )
    _user32.FlashWindowEx(ctypes.byref(info))
    return True
