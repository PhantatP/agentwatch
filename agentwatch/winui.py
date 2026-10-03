"""Taskbar flashing for the terminal window that owns a session.

A hook records its parent pid, which is the agent CLI. The window we want to
flash belongs to whatever terminal hosts it, so we walk up the process tree
and flash the first ancestor that owns a visible top-level window.
"""
import ctypes
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
    """pid -> hwnd for visible, unowned top-level windows."""
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
        found.setdefault(pid.value, hwnd)
        return True

    _user32.EnumWindows(_ENUMPROC(callback), 0)
    return found


def is_foreground(pid: int) -> bool:
    """Whether the window in front belongs to the terminal hosting `pid` --
    i.e. you are already looking at the session."""
    if not pid:
        return False
    owner = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(_user32.GetForegroundWindow(), ctypes.byref(owner))
    return owner.value in [pid] + proc.parent_chain(pid)


def flash_for_pid(pid: int, count: int = 4) -> bool:
    """Flash the terminal hosting `pid`. Returns whether a window was found."""
    if not pid:
        return False
    windows = _windows_by_pid()
    for candidate in [pid] + proc.parent_chain(pid):
        hwnd = windows.get(candidate)
        if not hwnd:
            continue
        info = _FLASHWINFO(
            cbSize=ctypes.sizeof(_FLASHWINFO),
            hwnd=hwnd,
            dwFlags=FLASHW_ALL | FLASHW_TIMERNOFG,
            uCount=count,
            dwTimeout=0,
        )
        _user32.FlashWindowEx(ctypes.byref(info))
        return True
    return False
