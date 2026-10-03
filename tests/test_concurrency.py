"""Hooks from several sessions -- and Codex's async ones -- can append to the
event log at the same moment, each from its own Python process."""
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WRITERS, EACH = 8, 150

# Each writer is a separate interpreter, like a real hook. It waits for a "go"
# file so they all start appending at once.
WRITER = r"""
import os, sys, time
sys.path.insert(0, sys.argv[1])
from agentwatch import store
go, writer = sys.argv[2], sys.argv[3]
while not os.path.exists(go):
    time.sleep(0.001)
for i in range(%d):
    store.append({"tool": "claude", "session": "w" + writer, "event": "PreToolUse", "n": i, "pad": "x" * 300})
""" % EACH


def test_concurrent_appends_lose_nothing(tmp_path):
    env = dict(os.environ, LOCALAPPDATA=str(tmp_path))
    go = tmp_path / "go"
    procs = [
        subprocess.Popen([sys.executable, "-c", WRITER, ROOT, str(go), str(w)], env=env)
        for w in range(WRITERS)
    ]
    time.sleep(1.0)  # let every interpreter start and reach the wait loop
    go.write_text("")
    assert all(proc.wait(60) == 0 for proc in procs)

    lines = (tmp_path / "agentwatch" / "events.jsonl").read_bytes().split(b"\n")[:-1]
    torn = [line[:80] for line in lines if not _parses(line)]
    assert torn == []
    assert len(lines) == WRITERS * EACH


def _parses(line: bytes) -> bool:
    try:
        json.loads(line)
        return True
    except ValueError:
        return False
