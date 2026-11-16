"""The tray indicator: one icon whose colour tracks every live agent session.

Runs as a single long-lived process so notifications cost nothing per event --
the hooks just append a line, and this reads it. Started automatically by the
first hook of a session, or manually with `python -m agentwatch.tray`.
"""
import ctypes
import json
import os
import sys
import threading
import time

from PIL import Image, ImageDraw, ImageFont
import pystray

from . import health, limits, panel, paths, proc, store, transcript, usage, winui

POLL_SECONDS = 1.0
USAGE_SECONDS = 20.0
DONE_GRACE = 3.0  # hold a "done" this long in case the next turn starts at once
WARN_PERCENT = 90  # toast once a limit window passes this
# A wait you got no toast for is reminded once it has gone unanswered this long.
REMIND_AFTER = {"permission": 60, "done": 180}

COLORS = {
    "running": (56, 132, 255),   # blue  -- agent is working (a tool not in TOOL_COLORS)
    "waiting": (255, 170, 0),    # amber -- agent wants you
    "idle": (46, 184, 114),      # green -- session open, turn finished
    "quiet": (130, 136, 148),    # grey  -- sessions open, nothing new
    "none": (130, 136, 148),     # grey  -- nothing running
}

# The running arc takes the colour of the company behind the tool at work, so
# you can tell which one it is. OpenAI's well-known green would read as the
# green "idle" ring, so Codex takes OpenAI's monochrome instead: off-white on a
# dark taskbar, near-black on a light one, where off-white would vanish.
# Order is the order of segments when several tools are working at once.
TOOL_COLORS = {
    "claude": (217, 119, 87),        # Claude orange, #D97757
    "codex": (236, 236, 241),        # OpenAI off-white, #ECECF1
    "antigravity": (66, 133, 244),   # Google blue, #4285F4
}
LIGHT_TASKBAR_COLORS = {"codex": (13, 13, 13)}  # OpenAI black, #0D0D0D

DEFAULT_SETTINGS = {
    "toasts": True,
    "flash": True,
    # What the panel shows.
    "show_sessions": True,
    "show_hooks": True,
    "show_claude": True,
    "show_codex": True,
    "show_limits": True,
    "show_tokens": True,
    "show_chart": True,
    "show_models": True,
}
SETTINGS_FILE = paths.DATA / "settings.json"


def load_settings() -> dict:
    merged = dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as fh:
            stored = json.load(fh)
        if isinstance(stored, dict):
            merged.update({k: v for k, v in stored.items() if k in DEFAULT_SETTINGS})
    except (OSError, json.JSONDecodeError):
        pass
    return merged


def save_settings(settings: dict) -> None:
    paths.ensure_data()
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as fh:
            json.dump(settings, fh)
    except OSError:
        pass


SUPERSAMPLE = 8  # draw large, downscale once -- Pillow has no antialiased primitives
BADGE_BG = (28, 30, 38, 255)


def _font(pixels: int):
    for name in ("segoeuib.ttf", "seguisb.ttf", "arialbd.ttf"):
        try:
            return ImageFont.truetype(name, pixels)
        except OSError:
            continue
    return ImageFont.load_default()


def make_icon(
    state: str, count: int, agents: int = 0, size: int = 64, tools=(), light: bool = False
) -> Image.Image:
    """A crisp state glyph. Shape carries the state as well as colour, so it
    still reads when the tray is small or the user cannot separate the hues."""
    scale = size * SUPERSAMPLE
    image = Image.new("RGBA", (scale, scale), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    color = COLORS.get(state, COLORS["none"]) + (255,)

    pad = scale * 0.09
    # With a badge the glyph shrinks toward the top-left, so the badge lands in
    # freed space instead of taking a bite out of it.
    inset = scale * 0.17 if count > 1 else 0.0
    box = (pad, pad, scale - pad - inset, scale - pad - inset)
    stroke = scale * 0.17

    if state == "running":
        # an open arc reads as motion; one segment per tool at work, in its colour
        palette = dict(TOOL_COLORS, **(LIGHT_TASKBAR_COLORS if light else {}))
        hues = [palette.get(tool, COLORS["running"]) + (255,) for tool in tools] or [color]
        span = 270 / len(hues)
        gap = 7  # degrees cut from each side of a seam, so adjacent hues stay apart
        # With a badge, turn the arc's opening to face it, so the badge covers
        # the gap instead of the last tool's segment.
        first = 90 if count > 1 else 135
        for i, hue in enumerate(hues):
            start = first + i * span + (gap if i else 0)
            end = first + (i + 1) * span - (gap if i < len(hues) - 1 else 0)
            draw.arc(box, start=start, end=end, fill=hue, width=int(stroke))
        if agents:
            # a core inside the arc: subagents are working too. One dot, not one
            # per agent -- more than one blurs together at 16px.
            centre, radius = (box[0] + box[2]) / 2, scale * 0.13
            draw.ellipse((centre - radius, centre - radius, centre + radius, centre + radius), fill=hues[0])
    elif state == "waiting":
        draw.ellipse(box, fill=color)  # solid: demands attention
    elif state == "idle":
        draw.ellipse(box, outline=color, width=int(stroke))  # calm ring
    else:
        draw.ellipse(box, outline=color, width=int(scale * 0.09))  # faint outline

    if count > 1:
        radius = scale * 0.27
        cx = cy = scale - radius
        # Draw operations replace pixels rather than blending, so a transparent
        # disc punches a clean gap between the badge and the glyph behind it.
        halo = radius * 1.2
        draw.ellipse((cx - halo, cy - halo, cx + halo, cy + halo), fill=(0, 0, 0, 0))
        draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=BADGE_BG)
        label = str(min(count, 9))
        font = _font(int(radius * 1.5))
        left, top, right, bottom = draw.textbbox((0, 0), label, font=font)
        draw.text(
            (cx - (right - left) / 2 - left, cy - (bottom - top) / 2 - top),
            label,
            font=font,
            fill=(255, 255, 255, 255),
        )

    return image.resize((size, size), Image.LANCZOS)


def short_age(since: float) -> str:
    seconds = max(0, int(time.time() - since))
    if seconds < 60:
        return "%ds" % seconds
    if seconds < 3600:
        return "%dm%02ds" % (seconds // 60, seconds % 60)
    return "%dh%02dm" % (seconds // 3600, (seconds % 3600) // 60)


LABEL = {"running": "running", "waiting": "needs you", "idle": "idle", "quiet": "idle"}
DOT = {"running": "◐", "waiting": "●", "idle": "○", "quiet": "○", "none": "○"}


# Toast wording, one (title, body) per notification kind. Phrased the way a
# person would say it: the title names who wants what, the body says where.
TOAST = {
    "done": ("%s is done", "Wrapped up in %s — it's back to you."),
    "permission": ("%s is asking first", "It wants your go-ahead before acting in %s."),
    "aborted": ("%s stopped", "You cut the turn short in %s."),
    "remind_done": ("%s is still waiting on you", "It finished in %s a few minutes ago."),
    "remind_permission": ("%s still needs your go-ahead", "A permission prompt in %s is unanswered."),
}


def agents_note(n: int) -> str:
    return " · %d agent%s" % (n, "" if n == 1 else "s") if n else ""


def tool_name(tool: str) -> str:
    return {"claude": "Claude", "codex": "Codex"}.get(tool, tool.capitalize())


def _fit(text: str, limit: int) -> str:
    """Trim on a word boundary. A toast clipped mid-word by Windows reads as a
    bug; an ellipsis reads as deliberate."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    space = cut.rfind(" ")
    if space > limit * 0.6:
        cut = cut[:space]
    return cut.rstrip(" ,.;:—-") + "…"


def coarse_age(since: float) -> str:
    """'just now', '4m', '1h05m' -- minutes at most, so the panel redraws once
    a minute for it, not every second."""
    minutes = max(0, int(time.time() - since)) // 60
    if not minutes:
        return "just now"
    if minutes < 60:
        return "%dm" % minutes
    return "%dh%02dm" % (minutes // 60, minutes % 60)


def project_of(sess) -> str:
    return os.path.basename((sess.cwd or "").rstrip("\\/")) or sess.cwd or "?"


# Tray-icon clicks, as pystray's window receives them.
WM_LBUTTONUP, WM_RBUTTONUP = 0x0202, 0x0205


class Tray:
    def __init__(self):
        self.sessions = store.Sessions()
        self.settings = load_settings()
        self.usage = None
        self.usage_error = ""
        self.claude_limits = limits.ClaudeLimits()
        self.first_poll = True
        self.held = []  # notifications not yet announced
        self.warned = set()  # (tool, window, reset time) already warned about
        self.notified = set()  # (tool, session, since): waits you were told about
        self.health = []  # [(tool, status, healthy)] for the HOOKS section
        self.log_errors = 0
        self.started = time.time()
        self.refreshing = threading.Lock()
        self.stopping = threading.Event()
        self.panel = panel.Panel(self)
        self.view = []  # the sessions as the panel lists them; see session_view
        try:
            # The sender's icon in toasts and in Settings: the tray's own glyph.
            paths.ensure_data()
            icon_file = paths.DATA / "icon.png"
            make_icon("idle", 1, size=128).save(icon_file)
            winui.register_app(str(icon_file))
        except Exception as exc:
            self._log("could not register for toasts: %r" % (exc,))
        self.icon = pystray.Icon("agentwatch", make_icon("none", 0), "agentwatch")
        # No menu: either button opens the panel. pystray looks its handlers up
        # per message, so the click handler can be swapped for ours.
        handlers = getattr(self.icon, "_message_handlers", None)
        if handlers is not None:
            from pystray._util import win32 as pystray_win32

            handlers[pystray_win32.WM_NOTIFY] = self.on_click

    def on_click(self, _wparam, lparam):
        if lparam in (WM_LBUTTONUP, WM_RBUTTONUP):
            # A tray click lets this process take the foreground. Claim it, so
            # the panel can take focus -- and close when you click elsewhere.
            hwnd = getattr(self.icon, "_hwnd", None)
            if hwnd:
                ctypes.windll.user32.SetForegroundWindow(hwnd)
            self.panel.toggle()
        return 0

    # ---------- what the panel shows ----------

    def session_view(self):
        """The sessions as the panel lists them. Built on the loop thread each
        tick, so the panel never walks the session table while it changes."""
        endable = {id(s) for s in self.idle_sessions()}
        rows = []
        for sess in self.sessions.active():
            lines = []
            if sess.status == "running":
                doing = transcript.activity(sess.transcript)
                if doing:
                    lines.append(_fit(doing, 52))
            for path in sess.agents.values():
                line = _fit(transcript.agent_label(path), 40)
                doing = transcript.activity(path)
                if doing:
                    line += " — " + _fit(doing, 28)
                lines.append("↳ " + line)
            for task, label in sess.background.items():
                if task not in sess.agents:  # a live agent already has its line
                    lines.append("↳ background · " + _fit(label, 40))
            rows.append({
                "sess": sess,
                "tool": sess.tool,
                "project": project_of(sess),
                "state": sess.state,
                "label": LABEL.get(sess.state, sess.state),
                "age": coarse_age(sess.since),
                "agents": len(sess.agents),
                "lines": lines,
                "can_end": id(sess) in endable,
            })
        return rows

    def reveal(self, sess: store.Session):
        """Clicking a session flashes the window that hosts it."""
        try:
            winui.flash_for_pid(sess.pid, sess.cwd, count=6)
        except Exception:
            pass

    def set_setting(self, key, value):
        self.settings[key] = value
        save_settings(self.settings)

    # ---------- ending idle sessions ----------

    def idle_sessions(self):
        """Sessions with nothing running that the tray can end: an agent CLI,
        not Antigravity, whose pid is the editor itself."""
        return [
            s for s in self.sessions.active()
            if s.state in ("idle", "quiet") and s.pid and s.tool in ("claude", "codex")
        ]

    def end_sessions(self, chosen):
        """End the agent CLI of each chosen session, after you confirm. The
        terminal or editor hosting it stays open, and the conversation can be
        resumed later."""
        threading.Thread(target=self._end, args=(list(chosen),), daemon=True).start()

    def _end(self, chosen):
        names = chr(10).join(
            "  %s  %s · idle %s" % (s.tool, project_of(s), short_age(s.since)) for s in chosen
        )
        what = "this session" if len(chosen) == 1 else "these %d sessions" % len(chosen)
        question = (
            "End %s?" % what + chr(10) * 2 + names + chr(10) * 2
            + "The CLI process and anything it started are stopped. "
            "The conversation is saved and can be resumed."
        )
        if not winui.confirm("agentwatch", question):
            return
        for sess in chosen:
            if sess.state not in ("idle", "quiet"):
                continue  # it started working while the question was open
            if proc.end_tree(sess.pid):
                self.sessions.by_key.pop((sess.tool, sess.sid), None)
            else:
                self._log("could not end %s session %s (pid %s)" % (sess.tool, sess.sid, sess.pid))

    def open_data_folder(self):
        paths.ensure_data()
        os.startfile(str(paths.DATA))  # noqa: S606 -- opening our own folder

    def refresh_in_background(self, force=False, max_age=limits.REFRESH_SECONDS):
        threading.Thread(target=self.refresh_usage, args=(force, max_age), daemon=True).start()

    def refresh_usage(self, force=False, max_age=limits.REFRESH_SECONDS):
        # One at a time: two scans at once would race on the usage cache file.
        if not self.refreshing.acquire(blocking=False):
            return
        try:
            try:
                self.usage = usage.collect()
                self.usage_error = ""
            except Exception as exc:
                self.usage_error = repr(exc)
            self.claude_limits.maybe_refresh(force=force, max_age=max_age)
            self.warn_limits()
            try:
                self.health = health.check(dict(self.sessions.last_event), limits.age)
                self.log_errors = health.log_errors()
            except Exception as exc:
                self._log("health check error: %r" % (exc,))
        finally:
            self.refreshing.release()

    def warn_limits(self):
        """Toast once when a limit window crosses WARN_PERCENT, then not again
        until it resets -- the meters are only seen with the menu open."""
        now = time.time()
        codex = limits.codex_windows((self.usage or {}).get("codex", {}).get("limits"))
        for tool, windows in (("claude", self.claude_limits.windows), ("codex", codex)):
            for window in windows:
                resets = window["resets_at"]
                key = (tool, window["label"], round(resets))
                if window["percent"] < WARN_PERCENT or key in self.warned:
                    continue
                if resets and resets <= now:
                    continue  # a stale Codex snapshot from a window already reset
                self.warned.add(key)
                if not self.settings["toasts"]:
                    continue
                title = "%s %s limit at %.0f%%" % (tool_name(tool), window["label"], window["percent"])
                body = "Resets in %s." % usage.human_until(resets) if resets else "Usage is nearly used up."
                try:
                    winui.toast(title, body)
                except Exception:
                    pass

    def quit(self):
        self.stopping.set()
        self.panel.stop()
        try:
            paths.TRAY_LOCK.unlink()
        except OSError:
            pass
        self.icon.stop()

    # ---------- notifications ----------

    def announce(self, event: dict, sess: store.Session):
        try:
            if winui.is_foreground(sess.pid, sess.cwd):
                return  # you are looking at it -- remind() follows up if not
        except Exception:
            pass
        self.notified.add((sess.tool, sess.sid, sess.since))
        self.deliver(store.NOTIFY_BY_EVENT[event.get("event", "")], sess, event.get("message") or "")

    def remind(self):
        """A second chance for a wait you were never told about: its window was
        in front, so it got no toast -- but in front is not the same as looked
        at -- or it was a Claude permission prompt, which never toasts. Once per
        wait, and only while nothing has changed since it began."""
        now = time.time()
        for sess in self.sessions.active():
            if sess.status == "waiting":
                kind, delay = "remind_permission", REMIND_AFTER["permission"]
            elif sess.status == "idle" and sess.last_event == "Stop" and not (sess.agents or sess.background):
                kind, delay = "remind_done", REMIND_AFTER["done"]
            else:
                continue  # working, interrupted by you, or just opened
            wait = (sess.tool, sess.sid, sess.since)
            if wait in self.notified or sess.since < self.started or now - sess.since < delay:
                continue
            self.notified.add(wait)
            self.deliver(kind, sess)

    def deliver(self, kind: str, sess: store.Session, message: str = ""):
        project = os.path.basename((sess.cwd or "").rstrip("\\/")) or "your session"
        title, body = TOAST[kind]
        # The CLI's own message is already a sentence; it just never says where.
        body = "%s — in %s" % (message.rstrip(". "), project) if message else body % project
        if self.settings["toasts"]:
            try:
                winui.toast(_fit(title % tool_name(sess.tool), 56), _fit(body, 170))
            except Exception:
                pass
        if self.settings["flash"] and kind != "aborted":
            try:
                winui.flash_for_pid(sess.pid, sess.cwd)
            except Exception:
                pass

    # ---------- loop ----------

    def tick(self):
        pending = self.sessions.poll()
        if self.first_poll:
            # Rebuild state from history without replaying old notifications.
            self.first_poll = False
            pending = [pair for pair in pending if pair[0].get("ts", 0) >= self.started]
        self.held += pending
        now = time.time()
        for event, sess in list(self.held):
            if event.get("event") == "Stop" and sess.last_event != "Stop":
                # A new turn began right after this Stop -- a queued message or
                # an agent's result waking the main agent -- so it is not done.
                self.held.remove((event, sess))
            elif event.get("event") != "Stop" or now - event.get("ts", now) >= DONE_GRACE:
                self.held.remove((event, sess))
                if event.get("event") == "Stop" and (sess.agents or sess.background):
                    continue  # work it was waiting on showed up during the grace
                self.announce(event, sess)

        self.sessions.reconcile()
        self.remind()
        self.view = self.session_view()
        state = self.sessions.aggregate()
        active = self.sessions.active()
        agents = self.sessions.agent_count()
        busy = agents + self.sessions.background_count()
        order = list(TOOL_COLORS)
        tools = sorted(
            {s.tool for s in active if s.state == "running"},
            key=lambda t: (order.index(t) if t in order else len(order), t),
        )
        try:
            light = winui.light_taskbar()
        except Exception:
            light = False
        self.icon.icon = make_icon(state, len(active), busy, tools=tools, light=light)
        # A Windows tooltip is capped at 127 characters, so build it in priority
        # order and stop before it would be cut mid-word.
        headline = {
            "running": "%d running" % sum(s.state == "running" for s in active),
            "waiting": "needs you",
            "idle": "%d idle" % len(active),
            "quiet": "%d idle" % len(active),
            "none": "no sessions",
        }.get(state, state)
        lines = ["agentwatch · " + headline + agents_note(agents)]
        for sess in active[:3]:
            project = os.path.basename(sess.cwd.rstrip("\\/")) or sess.cwd
            lines.append(
                "%s %s %s %s"
                % (DOT.get(sess.state, ""), project[:18], LABEL.get(sess.state, ""), short_age(sess.since))
            )
        percents = " · ".join(
            "%s %.0f%%" % (w["label"], w["percent"]) for w in self.claude_limits.windows[:2]
        ) if self.settings["show_claude"] else ""
        if percents:
            lines.append(percents)

        tooltip = ""
        for line in lines:
            candidate = line if not tooltip else tooltip + chr(10) + line
            if len(candidate) > 127:
                break
            tooltip = candidate
        self.icon.title = tooltip

    def run_loop(self):
        # Off the loop, so sessions show at once; the menu meanwhile shows the
        # limits cached from the last run rather than "loading…".
        self.refresh_in_background()
        self.panel.start()
        last_usage = time.time()
        while not self.stopping.is_set():
            try:
                self.tick()
                if time.time() - last_usage >= USAGE_SECONDS:
                    self.refresh_usage()
                    store.rotate_if_large()
                    last_usage = time.time()
            except Exception as exc:
                self._log("tick error: %r" % (exc,))
            self.stopping.wait(POLL_SECONDS)

    def _log(self, text):
        try:
            paths.ensure_data()
            with open(paths.LOG, "a", encoding="utf-8") as fh:
                fh.write(time.strftime("%Y-%m-%d %H:%M:%S ") + text + chr(10))
        except OSError:
            pass

    def run(self):
        threading.Thread(target=self.run_loop, daemon=True).start()
        self.icon.run()


def already_running() -> bool:
    from . import proc

    try:
        pid = int(paths.TRAY_LOCK.read_text().strip())
    except (OSError, ValueError):
        return False
    return proc.python_alive(pid) and pid != os.getpid()


def main() -> int:
    paths.ensure_data()
    if already_running():
        return 0
    paths.TRAY_LOCK.write_text(str(os.getpid()), encoding="utf-8")
    tray = Tray()
    try:
        tray.run()
    finally:
        try:
            if paths.TRAY_LOCK.read_text().strip() == str(os.getpid()):
                paths.TRAY_LOCK.unlink()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
