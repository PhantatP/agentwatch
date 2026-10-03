"""Append-only event log and the session state derived from it.

Hooks only append; the tray derives everything. That keeps hook latency low,
which matters because a hook blocks the CLI turn while it runs.
"""
import json
import os
import time
from dataclasses import dataclass, field

from . import paths, proc, transcript

MAX_EVENTS_BYTES = 512 * 1024
NEWLINE = bytes([10])

# event -> session status. Events not listed leave status unchanged.
STATUS_BY_EVENT = {
    "SessionStart": "idle",
    "UserPromptSubmit": "running",
    "PreToolUse": "running",
    "PostToolUse": "running",
    "PreInvocation": "running",  # Antigravity: before each model call
    "PermissionRequest": "waiting",
    "Notification": "waiting",
    "Stop": "idle",
    "Interrupt": "idle",
    "SessionEnd": "ended",
}

# event -> notification kind. Absent means "no notification". The wording for
# each kind lives in the tray, which is the only thing that shows it.
NOTIFY_BY_EVENT = {
    "Stop": "done",
    "PermissionRequest": "permission",
    "Interrupt": "aborted",
}


def append(event: dict) -> None:
    paths.ensure_data()
    event.setdefault("ts", time.time())
    line = json.dumps(event, ensure_ascii=False) + "\n"
    with open(paths.EVENTS, "a", encoding="utf-8") as fh:
        fh.write(line)


def rotate_if_large() -> None:
    """Keep the tail only. Called by the tray, never by a hook."""
    try:
        if paths.EVENTS.stat().st_size <= MAX_EVENTS_BYTES:
            return
    except OSError:
        return
    lines = paths.EVENTS.read_text(encoding="utf-8", errors="replace").splitlines()
    keep = lines[-2000:]
    tmp = paths.EVENTS.with_suffix(".tmp")
    tmp.write_text("\n".join(keep) + "\n", encoding="utf-8")
    os.replace(tmp, paths.EVENTS)


def read_events(offset: int = 0):
    """Yield (new_offset, events), reading only bytes appended since offset.

    Binary mode: byte offsets stay meaningful, and a half-written final line is
    left for the next poll instead of being parsed as truncated JSON.
    """
    try:
        size = paths.EVENTS.stat().st_size
    except OSError:
        return 0, []
    if size < offset:  # file was rotated or replaced
        offset = 0
    if size == offset:
        return offset, []
    with open(paths.EVENTS, "rb") as fh:
        fh.seek(offset)
        data = fh.read()
    cut = data.rfind(NEWLINE)
    if cut == -1:
        return offset, []
    complete, new_offset = data[: cut + 1], offset + cut + 1
    out = []
    for line in complete.decode("utf-8", "replace").splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return new_offset, out


@dataclass
class Session:
    tool: str
    sid: str
    cwd: str = ""
    pid: int = 0
    model: str = ""
    transcript: str = ""
    status: str = "idle"
    since: float = field(default_factory=time.time)
    last_event: str = ""
    agents: dict = field(default_factory=dict)  # mid-turn subagent id -> transcript

    @property
    def state(self) -> str:
        """Status as shown. A turn that ended while its background agents
        still work has not really finished, so it reads as running."""
        return "running" if self.agents and self.status == "idle" else self.status


class Sessions:
    """Live session table, fed incrementally from the event log."""

    STALE_AFTER = 12 * 3600

    def __init__(self):
        self.by_key: dict[tuple[str, str], Session] = {}
        self.offset = 0

    def poll(self):
        """Read new events. Returns the list of (event, session) to notify on."""
        self.offset, events = read_events(self.offset)
        notifications = []
        for ev in events:
            tool, sid = ev.get("tool", "?"), ev.get("session", "?")
            key = (tool, sid)
            name = ev.get("event", "")
            sess = self.by_key.get(key)
            if sess is None:
                sess = Session(tool=tool, sid=sid)
                self.by_key[key] = sess
            for attr in ("cwd", "pid", "model", "transcript"):
                if ev.get(attr):
                    setattr(sess, attr, ev[attr])
            status = STATUS_BY_EVENT.get(name)
            if status and status != sess.status:
                sess.status = status
                sess.since = ev.get("ts", time.time())
            sess.last_event = name
            if name == "Stop":
                sess.agents = transcript.live_agents(sess.transcript)
            if status == "ended":
                self.by_key.pop(key, None)
            elif name == "Stop" and sess.agents:
                # The main agent paused to wait on its agents -- or woke to read
                # one agent's result. Only the Stop after the last one is "done".
                continue
            elif name in NOTIFY_BY_EVENT:
                notifications.append((ev, sess))
        self._drop_stale()
        return notifications

    def reconcile(self):
        """Correct `running` sessions that actually stopped.

        Neither CLI emits an event when you hit Esc or close the terminal, so
        without this a stopped session shows as working forever. Two independent
        checks: the owning process is gone, or the transcript says the turn
        ended. Never promotes a session back to running -- events do that.
        """
        for key, sess in list(self.by_key.items()):
            if sess.pid and not proc.process_alive(sess.pid):
                del self.by_key[key]
                continue
            sess.agents = transcript.live_agents(sess.transcript)
            if sess.status != "running":
                continue
            # Claude writes the new prompt ~140ms after the UserPromptSubmit
            # hook, so just after it the tail still holds the previous turn's
            # end. Only an end written after this turn began counts.
            ended = transcript.ended_at(sess.transcript)
            if ended is not None and ended > sess.since:
                sess.status = "idle"
                sess.since = time.time()

    def _drop_stale(self):
        cutoff = time.time() - self.STALE_AFTER
        for key, sess in list(self.by_key.items()):
            if sess.since < cutoff:
                del self.by_key[key]

    def active(self):
        order = {"waiting": 0, "running": 1, "idle": 2}
        return sorted(self.by_key.values(), key=lambda s: (order.get(s.state, 3), -s.since))

    def agent_count(self) -> int:
        return sum(len(s.agents) for s in self.by_key.values())

    def aggregate(self) -> str:
        statuses = {s.state for s in self.by_key.values()}
        if "waiting" in statuses:
            return "waiting"
        if "running" in statuses:
            return "running"
        if statuses:
            return "idle"
        return "none"
