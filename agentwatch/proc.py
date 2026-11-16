"""Small Win32 helpers that avoid a psutil dependency."""
import ctypes
import subprocess
from ctypes import wintypes

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)

SYNCHRONIZE = 0x00100000
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259


def process_alive(pid: int) -> bool:
    if not pid:
        return False
    handle = _k32.OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if _k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return code.value == STILL_ACTIVE
        return True
    finally:
        _k32.CloseHandle(handle)


def process_name(pid: int) -> str:
    """Executable name for a pid, or '' if it cannot be read."""
    handle = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(260)
        buf = ctypes.create_unicode_buffer(size.value)
        if _k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value.rsplit("\\", 1)[-1]
        return ""
    finally:
        _k32.CloseHandle(handle)


def python_alive(pid: int) -> bool:
    """A live Python process. Windows reuses pids, so for the tray's lock file
    "alive" alone could mean an unrelated program now holds the old tray's pid
    -- and the tray would never start again."""
    return process_alive(pid) and process_name(pid).lower() in ("python.exe", "pythonw.exe")


# What a session's pid may be when the tray is asked to end it. The hook records
# the agent CLI itself; anything else means Windows reused the pid, and ending
# it would kill an unrelated program.
AGENT_EXES = ("claude.exe", "codex.exe", "node.exe")


def end_tree(pid: int) -> bool:
    """End an agent CLI and everything it started (shells, MCP servers) --
    never its parents, so the terminal or editor hosting it stays open.
    Refuses a pid that is not an agent CLI. Returns whether it was ended."""
    if not process_alive(pid) or process_name(pid).lower() not in AGENT_EXES:
        return False
    CREATE_NO_WINDOW = 0x08000000
    done = subprocess.run(
        ["taskkill", "/PID", str(int(pid)), "/T", "/F"],
        capture_output=True,
        creationflags=CREATE_NO_WINDOW,
    )
    return done.returncode == 0


class _PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_char * 260),
    ]


def parent_chain(pid: int, depth: int = 8) -> list[int]:
    """Walk up the process tree, nearest ancestor first."""
    TH32CS_SNAPPROCESS = 0x00000002
    snap = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == -1:
        return []
    parents: dict[int, int] = {}
    try:
        entry = _PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32)
        if _k32.Process32First(snap, ctypes.byref(entry)):
            while True:
                parents[entry.th32ProcessID] = entry.th32ParentProcessID
                if not _k32.Process32Next(snap, ctypes.byref(entry)):
                    break
    finally:
        _k32.CloseHandle(snap)
    chain, seen = [], {pid}
    current = pid
    for _ in range(depth):
        current = parents.get(current, 0)
        if not current or current in seen:
            break
        seen.add(current)
        chain.append(current)
    return chain
