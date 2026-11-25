"""The panel: agentwatch's whole interface, opened by clicking the tray icon.

Sessions (and ending idle ones), usage limits as bars, token totals, a 7-day
chart, a per-model split, hook health, and the settings -- each section can be
hidden. Tk must live on one thread, so the panel owns a thread with its own Tk
root; the tray talks to it only through a queue, and the panel reads only
snapshots the tray's loop has finished building.
"""

import ctypes
import ctypes.wintypes
import queue
import threading
import time
import tkinter as tk
from datetime import datetime, timedelta

from . import limits, usage, winui

# Chart colours, validated (dataviz validator) against each surface. The tray's
# Codex off-white/black reads as grey in a chart, so the panel uses a violet.
# waiting/idle/antigravity mark session states, as the tray icon does.
THEMES = {
    "dark": {
        "bg": "#1a1a19",
        "card": "#242422",
        "ink": "#ecebe8",
        "muted": "#9b9993",
        "line": "#3a3936",
        "track": "#34332f",
        "claude": "#C96A48",
        "codex": "#8277E6",
        "antigravity": "#4285F4",
        "waiting": "#e5a33b",
        "idle": "#3fb871",
        "critical": "#e5534b",
    },
    "light": {
        "bg": "#fcfcfb",
        "card": "#f2f1ee",
        "ink": "#1f1e1c",
        "muted": "#6b6963",
        "line": "#e2e0db",
        "track": "#e6e4df",
        "claude": "#C2603C",
        "codex": "#6E62D6",
        "antigravity": "#2f6fdc",
        "waiting": "#b7791f",
        "idle": "#2f8f57",
        "critical": "#c93a33",
    },
}
APPS = (("claude", "Claude"), ("codex", "Codex"))
SECTIONS = (
    ("show_sessions", "Sessions"),
    ("show_limits", "Limits"),
    ("show_tokens", "Tokens"),
    ("show_chart", "Last 7 days"),
    ("show_models", "By model"),
    ("show_hooks", "Hooks"),
)
NOTIFY = (("toasts", "Toasts"), ("flash", "Flash taskbar"))
DOT = {"running": "◐", "waiting": "●", "idle": "○", "quiet": "○"}
WIDTH = 360  # at 96 DPI; scaled to the monitor
FONT = "Segoe UI"


def _work_area():
    """(left, top, right, bottom) of the desktop minus the taskbar."""
    rect = ctypes.wintypes.RECT()
    ctypes.windll.user32.SystemParametersInfoW(
        0x30, 0, ctypes.byref(rect), 0
    )  # SPI_GETWORKAREA
    return rect.left, rect.top, rect.right, rect.bottom


def week_days(now=None):
    """The last 7 local dates, oldest first."""
    now = now or datetime.now()
    return [(now - timedelta(days=back)) for back in range(6, -1, -1)]


def signature(tray, customizing=False) -> str:
    """Everything the panel shows, as text. It redraws only when this changes:
    rebuilding on every refresh made it blink while you looked at it."""
    u = tray.usage or {}
    parts = [
        sorted(tray.settings.items()),
        customizing,
        [
            (r["tool"], r["where"], r["project"], r["state"], r["age"], r["lines"], r["can_end"])
            for r in tray.view
        ],
        [
            (w["label"], round(w["percent"]), usage.human_until(w["resets_at"]))
            for w in tray.claude_limits.windows
        ],
        tray.claude_limits.error,
        [
            (w["label"], round(w["percent"]), limits.age(w.get("as_of", 0)))
            for w in limits.codex_windows(u.get("codex", {}).get("limits"))
        ],
        limits.age(u.get("generated_at", 0)),
        tray.usage_error,
        tray.health,
        tray.log_errors,
    ]
    for app in ("claude", "codex"):
        data = u.get(app) or {}
        parts.append([usage.human_tokens(data.get(k, 0)) for k in ("today", "week")])
        parts.append(usage.human_tokens((data.get("block") or {}).get("tokens", 0)))
        parts.append(
            [
                usage.human_tokens(v)
                for _d, v in sorted((data.get("daily") or {}).items())[-8:]
            ]
        )
        parts.append(sorted((data.get("models") or {}).get("week", {}).items()))
    return repr(parts)


class Panel:
    def __init__(self, tray):
        self.tray = tray
        self.inbox = queue.Queue()
        self.thread = None
        self.root = self.win = self.body = self.inner = None
        self.shown = None  # signature of what is on screen
        self.size = None
        self.customizing = False
        self.hidden_at = 0.0

    # ---------- called from the tray's threads ----------

    def start(self):
        """Create Tk ahead of the first click, so the first open is instant."""
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()

    def toggle(self):
        self.start()
        self.inbox.put("toggle")

    def stop(self):
        """Close Tk and wait for it. Tk objects freed from another thread, as
        happens at interpreter exit, abort the process."""
        self.inbox.put("quit")
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=3)

    # ---------- the Tk thread ----------

    def _run(self):
        try:
            # Sharp text on a scaled display. Per thread, so the tray's own
            # window, created on another thread, is left as it was.
            ctypes.windll.user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        except (AttributeError, OSError):
            pass
        self.root = tk.Tk()
        self.root.withdraw()
        self.scale = self.root.winfo_fpixels("1i") / 96.0
        self.root.after(50, self._pump)
        self.root.mainloop()
        self.root = self.win = self.body = self.inner = None  # freed on this thread

    def px(self, value) -> int:
        return int(round(value * self.scale))

    def _pump(self):
        try:
            while True:
                message = self.inbox.get_nowait()
                if message == "quit":
                    self.root.destroy()
                    return
                if message == "toggle":
                    self._toggle()
        except queue.Empty:
            pass
        if (
            self.win is not None
            and signature(self.tray, self.customizing) != self.shown
        ):
            self.render()
        self.root.after(250, self._pump)

    def _toggle(self):
        if self.win is not None:
            self.hide()
        elif time.time() - self.hidden_at > 0.4:
            # A click on the tray icon first takes focus from the panel, which
            # hides it; that same click must not reopen it.
            self.show()

    def show(self):
        light = False
        try:
            light = winui.light_taskbar()
        except OSError:
            pass
        self.c = THEMES["light" if light else "dark"]
        self.win = tk.Toplevel(self.root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.configure(bg=self.c["line"])  # a 1px border around the body
        self.win.bind("<Escape>", lambda _e: self.hide())
        self.win.bind("<FocusOut>", lambda _e: self.root.after(120, self._maybe_hide))
        self.body = tk.Frame(self.win, bg=self.c["bg"])
        self.body.pack(fill="both", expand=True, padx=1, pady=1)
        self.inner, self.size = None, None
        self.render()
        self.win.focus_force()
        self.tray.refresh_in_background(max_age=60)  # fresh numbers while you look

    def _maybe_hide(self):
        if self.win is not None and self.win.focus_displayof() is None:
            self.hide()

    def hide(self):
        if self.win is not None:
            self.win.destroy()
            self.win = self.body = self.inner = None
            self.hidden_at = time.time()

    def _place(self):
        self.win.update_idletasks()
        size = (self.win.winfo_reqwidth(), self.win.winfo_reqheight())
        if size == self.size:
            return  # moving a window that has not changed size makes it flicker
        self.size = size
        _left, top, right, bottom = _work_area()
        margin = self.px(12)
        # Taller than the screen, keep the header (and its ✕) in view; hiding
        # sections with ⚙ shortens it.
        y = max(top + margin, bottom - size[1] - margin)
        self.win.geometry("+%d+%d" % (right - size[0] - margin, y))

    # ---------- content ----------

    def render(self):
        """Build the new content off-screen, then swap it in, so the panel
        never shows half-drawn."""
        self.shown = signature(self.tray, self.customizing)
        old = self.inner
        pad = self.px(16)
        self.inner = tk.Frame(self.body, bg=self.c["bg"], width=self.px(WIDTH))
        settings = self.tray.settings
        self._header()
        if self.customizing:
            self._chips(SECTIONS, "%s", dot=False)
        if settings.get("show_sessions"):
            self._sessions()
        self._section("Usage")
        self._chips(APPS, "show_%s", dot=True, top=0)
        apps = [(key, name) for key, name in APPS if settings.get("show_" + key)]
        if not apps:
            self._note("Pick an app above to see its usage.")
        else:
            if settings.get("show_limits"):
                self._limits(apps)
            if settings.get("show_tokens") and self.tray.usage:
                self._tokens(apps)
            if settings.get("show_chart") and self.tray.usage:
                self._chart(apps)
            if (
                settings.get("show_models")
                and self.tray.usage
                and settings.get("show_claude")
            ):
                self._models()
            if self.tray.usage_error:
                self._note("Token totals unavailable: " + self.tray.usage_error[:60])
        if settings.get("show_hooks"):
            self._hooks()
        self._footer()
        if old is not None:
            old.destroy()
        self.inner.pack(fill="both", expand=True, padx=pad, pady=(self.px(12), pad))
        self._place()

    def _label(self, parent, text, size=9, color="ink", bold=False, **kw):
        font = (
            (FONT + (" Semibold" if bold else ""), size)
            if not isinstance(bold, tuple)
            else bold
        )
        return tk.Label(
            parent,
            text=text,
            font=font,
            fg=self.c[color],
            bg=kw.pop("bg", self.c["bg"]),
            **kw,
        )

    def _link(self, parent, text, action, color="muted"):
        """Clickable text, underlined so it reads as something to click."""
        link = self._label(
            parent, text, 9, color, bold=(FONT, 9, "underline"), cursor="hand2"
        )
        link.bind("<Button-1>", lambda _e: action())
        return link

    def _note(self, text):
        self._label(
            self.inner, text, 8, "muted", wraplength=self.px(WIDTH), justify="left"
        ).pack(anchor="w", pady=(self.px(10), 0))

    def _header(self):
        row = tk.Frame(self.inner, bg=self.c["bg"])
        row.pack(fill="x")
        self._label(row, "agentwatch", 12, bold=True).pack(side="left")
        for glyph, action in (
            ("✕", self.hide),
            ("⚙", self._customize),
            ("⟳", self._refresh),
        ):
            button = self._label(
                row, glyph, 11, "muted", cursor="hand2", padx=self.px(4)
            )
            button.pack(side="right")
            button.bind("<Button-1>", lambda _e, act=action: act())
        stamp = (self.tray.usage or {}).get("generated_at", 0)
        self._label(
            self.inner, "usage updated %s" % limits.age(stamp), 8, "muted"
        ).pack(anchor="w")

    def _refresh(self):
        self.tray.refresh_in_background(force=True)

    def _customize(self):
        self.customizing = not self.customizing
        self.render()

    def _chips(self, options, key_format, dot, top=10):
        """A row (or rows) of on/off pills, each flipping one setting."""
        row = tk.Frame(self.inner, bg=self.c["bg"])
        row.pack(fill="x", pady=(self.px(top), 0))
        used = 0
        for key, name in options:
            setting = key_format % key
            on = bool(self.tray.settings.get(setting))
            mark = ("●" if on else "○") if dot else ("✓" if on else "+")
            fill = self.c["card"] if on else self.c["bg"]
            if used > WIDTH - 90:  # wrap before a pill would run off the edge
                row = tk.Frame(self.inner, bg=self.c["bg"])
                row.pack(fill="x", pady=(self.px(6), 0))
                used = 0
            used += 30 + 7 * len(name)
            chip = tk.Frame(
                row,
                bg=fill,
                highlightthickness=1,
                highlightbackground=self.c["line"],
                padx=self.px(8),
                pady=self.px(2),
                cursor="hand2",
            )
            # The mark carries the app's colour; the name stays in ink.
            tint = key if (dot and on) else ("ink" if on else "muted")
            parts = (
                self._label(chip, mark, 9, tint, bg=fill),
                self._label(chip, name, 9, "ink" if on else "muted", bg=fill),
            )
            parts[0].pack(side="left")
            parts[1].pack(side="left", padx=(self.px(4), 0))
            chip.pack(side="left", padx=(0, self.px(6)))
            for widget in (chip,) + parts:
                widget.bind("<Button-1>", lambda _e, s=setting: self._flip(s))

    def _flip(self, setting):
        self.tray.set_setting(setting, not self.tray.settings.get(setting))
        self.render()

    def _section(self, title):
        self._label(self.inner, title.upper(), 8, "muted", bold=True).pack(
            anchor="w", pady=(self.px(16), self.px(4))
        )

    def _bar(self, parent, fraction, color, width, height=6):
        canvas = tk.Canvas(
            parent,
            width=self.px(width),
            height=self.px(height + 4),
            bg=self.c["bg"],
            highlightthickness=0,
        )
        thick, y = self.px(height), self.px(height + 4) / 2
        x0, x1 = thick / 2, self.px(width) - thick / 2
        canvas.create_line(
            x0, y, x1, y, fill=self.c["track"], width=thick, capstyle="round"
        )
        if fraction > 0:
            end = x0 + max(fraction, 0.0) * (x1 - x0)
            canvas.create_line(
                x0, y, max(end, x0 + 0.5), y, fill=color, width=thick, capstyle="round"
            )
        return canvas

    def _sessions(self):
        self._section("Sessions")
        view = self.tray.view
        if not view:
            self._label(self.inner, "Nothing running", 9, "muted").pack(anchor="w")
            return
        for row in view:
            sess = row["sess"]
            line = tk.Frame(self.inner, bg=self.c["bg"], cursor="hand2")
            line.pack(fill="x", pady=(self.px(2), 0))
            state = row["state"]
            tint = (
                row["tool"] if state == "running" and row["tool"] in self.c else state
            )
            tint = tint if tint in self.c else "muted"
            clickable = [
                self._label(line, DOT.get(state, "○"), 10, tint),
                self._label(line, row["project"], 9, bold=True, padx=self.px(4)),
                self._label(line, " · ".join(filter(None, (row["tool"], row["where"]))), 8, "muted"),
            ]
            for widget in clickable:
                widget.pack(side="left")
            if row["can_end"]:
                self._link(
                    line, "End", lambda s=sess: self.tray.end_sessions([s])
                ).pack(side="right")
            note = "%s · %s" % (row["label"], row["age"])
            if row["agents"]:
                note += " · %d agent%s" % (
                    row["agents"],
                    "" if row["agents"] == 1 else "s",
                )
            status = self._label(
                line,
                note,
                8,
                "waiting" if state == "waiting" else "muted",
                padx=self.px(6),
            )
            status.pack(side="right")
            for text in row["lines"]:
                detail = self._label(
                    self.inner,
                    text,
                    8,
                    "muted",
                    anchor="w",
                    padx=self.px(18),
                    cursor="hand2",
                )
                detail.pack(fill="x")
                clickable.append(detail)
            # Clicking a session flashes the window that hosts it.
            for widget in clickable + [line, status]:
                widget.bind("<Button-1>", lambda _e, s=sess: self.tray.reveal(s))
        idle = [r["sess"] for r in view if r["can_end"]]
        if len(idle) > 1:
            self._link(
                self.inner,
                "End all %d idle sessions" % len(idle),
                lambda: self.tray.end_sessions(idle),
            ).pack(anchor="e", pady=(self.px(4), 0))

    def _limits(self, apps):
        self._section("Limits")
        rows = []
        for key, name in apps:
            if key == "claude":
                lim = self.tray.claude_limits
                windows = [
                    dict(w, note="resets " + usage.human_until(w["resets_at"]))
                    for w in lim.windows
                ]
                empty = lim.error or "loading…"
            else:
                snapshot = (self.tray.usage or {}).get("codex", {}).get("limits")
                windows = [
                    dict(w, note=limits.age(w.get("as_of", 0)))
                    for w in limits.codex_windows(snapshot)
                ]
                empty = "no session recorded yet"
            rows.append((key, name, windows, empty))
        grid = tk.Frame(self.inner, bg=self.c["bg"])
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)
        r = 0
        for key, name, windows, empty in rows:
            if not windows:
                self._label(grid, "%s  %s" % (name, empty), 9, "muted").grid(
                    row=r, column=0, columnspan=4, sticky="w"
                )
                r += 1
                continue
            for window in windows:
                percent = window["percent"]
                hot = percent >= 90
                label = "%s %s" % (name, window["label"])
                self._label(grid, label, 9).grid(
                    row=r, column=0, sticky="w", padx=(0, self.px(8))
                )
                self._bar(
                    grid,
                    percent / 100.0,
                    self.c["critical"] if hot else self.c[key],
                    120,
                ).grid(row=r, column=1, sticky="w")
                text = ("⚠ " if hot else "") + "%.0f%%" % percent
                self._label(
                    grid,
                    text,
                    9,
                    "critical" if hot else "ink",
                    bold=True,
                    width=6,
                    anchor="e",
                ).grid(row=r, column=2, sticky="e")
                self._label(grid, window["note"], 8, "muted", anchor="e").grid(
                    row=r, column=3, sticky="e", padx=(self.px(6), 0)
                )
                r += 1

    def _tokens(self, apps):
        self._section("Tokens")
        grid = tk.Frame(self.inner, bg=self.c["bg"])
        grid.pack(fill="x")
        grid.columnconfigure(0, weight=1)
        for col, head in enumerate(("today", "7 days", "5h block"), start=1):
            self._label(grid, head, 8, "muted", anchor="e", width=8).grid(
                row=0, column=col, sticky="e"
            )
        for r, (key, name) in enumerate(apps, start=1):
            data = self.tray.usage.get(key, {})
            cell = tk.Frame(grid, bg=self.c["bg"])
            cell.grid(row=r, column=0, sticky="w")
            self._label(cell, "●", 9, key).pack(side="left")
            self._label(cell, name, 9).pack(side="left", padx=(self.px(4), 0))
            values = (
                data.get("today", 0),
                data.get("week", 0),
                (data.get("block") or {}).get("tokens", 0),
            )
            for col, value in enumerate(values, start=1):
                self._label(
                    grid, usage.human_tokens(value), 10, bold=True, anchor="e", width=8
                ).grid(row=r, column=col, sticky="e")

    def _chart(self, apps):
        self._section("Last 7 days")
        days = week_days()
        stacks = []
        for day in days:
            stamp = day.strftime("%Y-%m-%d")
            stacks.append(
                [
                    (
                        key,
                        name,
                        self.tray.usage.get(key, {}).get("daily", {}).get(stamp, 0),
                    )
                    for key, name in apps
                ]
            )
        if len(apps) > 1:  # a legend for two series; one needs none
            legend = tk.Frame(self.inner, bg=self.c["bg"])
            legend.pack(anchor="w", pady=(0, self.px(4)))
            for key, name in apps:
                self._label(legend, "■", 9, key).pack(side="left")
                self._label(legend, name, 8, "muted").pack(
                    side="left", padx=(self.px(2), self.px(10))
                )

        width, height = self.px(WIDTH), self.px(118)
        top, bottom = self.px(16), height - self.px(20)
        canvas = tk.Canvas(
            self.inner,
            width=width,
            height=height,
            bg=self.c["bg"],
            highlightthickness=0,
        )
        canvas.pack(anchor="w")
        totals = [sum(v for _k, _n, v in stack) for stack in stacks]
        peak = max(totals) or 1
        slot = width / 7.0
        bar = min(self.px(26), slot * 0.6)
        gap = self.px(2)
        canvas.create_line(0, bottom + 0.5, width, bottom + 0.5, fill=self.c["line"])
        label_at = {totals.index(max(totals)), 6} if max(totals) else set()
        for i, stack in enumerate(stacks):
            x0 = i * slot + (slot - bar) / 2
            x1 = x0 + bar
            y = bottom
            drawn = [(key, value) for key, _n, value in stack if value > 0]
            for j, (key, value) in enumerate(drawn):
                h = (bottom - top) * value / peak
                y_top = y - h
                self._column(
                    canvas, x0, y_top, x1, y, self.c[key], rounded=j == len(drawn) - 1
                )
                y = y_top - gap  # a surface gap between stacked segments
            if i in label_at:  # label the peak and today, not every bar
                canvas.create_text(
                    (x0 + x1) / 2,
                    y - self.px(2),
                    text=usage.human_tokens(totals[i]),
                    anchor="s",
                    fill=self.c["ink"],
                    font=(FONT, 8),
                )
            day = "Today" if i == 6 else days[i].strftime("%a")
            canvas.create_text(
                (x0 + x1) / 2,
                bottom + self.px(10),
                text=day,
                fill=self.c["ink" if i == 6 else "muted"],
                font=(FONT + (" Semibold" if i == 6 else ""), 8),
            )

        def hover(event):
            canvas.delete("tip")
            i = min(6, max(0, int(event.x // slot)))
            lines = [days[i].strftime("%a %d %b")] + [
                "%s  %s" % (name, usage.human_tokens(value))
                for _k, name, value in stacks[i]
            ]
            text = canvas.create_text(
                0,
                0,
                text="\n".join(lines),
                anchor="nw",
                fill=self.c["ink"],
                font=(FONT, 8),
                tags="tip",
            )
            x0, y0, x1, y1 = canvas.bbox(text)
            w, h = x1 - x0 + self.px(12), y1 - y0 + self.px(8)
            x = min(max(event.x - w / 2, 0), width - w)
            canvas.coords(text, x + self.px(6), self.px(2) + self.px(4))
            box = canvas.create_rectangle(
                x,
                self.px(2),
                x + w,
                self.px(2) + h,
                fill=self.c["card"],
                outline=self.c["line"],
                tags="tip",
            )
            canvas.tag_raise(text, box)

        canvas.bind("<Motion>", hover)
        canvas.bind("<Leave>", lambda _e: canvas.delete("tip"))

    def _column(self, canvas, x0, y0, x1, y1, color, rounded):
        """A bar segment; the top one gets a rounded data-end."""
        r = min(self.px(3), (x1 - x0) / 2, max(0.0, y1 - y0) / 2) if rounded else 0
        canvas.create_rectangle(x0, y0 + r, x1, y1, fill=color, width=0)
        if r:
            canvas.create_rectangle(x0 + r, y0, x1 - r, y0 + r, fill=color, width=0)
            canvas.create_oval(x0, y0, x0 + 2 * r, y0 + 2 * r, fill=color, width=0)
            canvas.create_oval(x1 - 2 * r, y0, x1, y0 + 2 * r, fill=color, width=0)

    def _models(self):
        models = (self.tray.usage.get("claude") or {}).get("models") or {}
        week, today = models.get("week") or {}, models.get("today") or {}
        if not week:
            return
        self._section("Claude by model")
        grid = tk.Frame(self.inner, bg=self.c["bg"])
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)
        for col, head in ((2, "today"), (3, "7 days")):
            self._label(grid, head, 8, "muted", anchor="e", width=7).grid(
                row=0, column=col, sticky="e"
            )
        total = sum(week.values()) or 1
        ranked = sorted(week.items(), key=lambda kv: -kv[1])
        for r, (family, tokens) in enumerate(ranked, start=1):
            self._label(grid, family, 9).grid(
                row=r, column=0, sticky="w", padx=(0, self.px(8))
            )
            self._bar(grid, tokens / total, self.c["claude"], 110, height=5).grid(
                row=r, column=1, sticky="w"
            )
            self._label(
                grid, usage.human_tokens(today.get(family, 0)), 9, anchor="e", width=7
            ).grid(row=r, column=2, sticky="e")
            self._label(
                grid, usage.human_tokens(tokens), 9, bold=True, anchor="e", width=7
            ).grid(row=r, column=3, sticky="e")
        self._note(
            "Totals include cache reads, so they run far above what your plan meters."
        )

    def _hooks(self):
        """Hooks fail silently by design, so a broken one looks like an idle
        tool. This says which: last event per tool, or why it is quiet."""
        if not (self.tray.health or self.tray.log_errors):
            return
        self._section("Hooks")
        for tool, status, healthy in self.tray.health:
            row = tk.Frame(self.inner, bg=self.c["bg"])
            row.pack(fill="x", pady=(0, self.px(2)))
            self._label(
                row,
                "✓" if healthy else "⚠",
                9,
                "idle" if healthy else "critical",
                width=2,
                anchor="w",
            ).pack(side="left", anchor="n")
            self._label(row, tool, 9, bold=True, width=7, anchor="w").pack(
                side="left", anchor="n"
            )
            self._label(
                row,
                status,
                8,
                "muted" if healthy else "ink",
                wraplength=self.px(WIDTH - 90),
                justify="left",
                anchor="w",
            ).pack(side="left", fill="x")
        if self.tray.log_errors:
            count = self.tray.log_errors
            self._link(
                self.inner,
                "⚠ %d error%s in agentwatch.log" % (count, "" if count == 1 else "s"),
                self.tray.open_data_folder,
                color="critical",
            ).pack(anchor="w")

    def _footer(self):
        tk.Frame(self.inner, bg=self.c["line"], height=1).pack(
            fill="x", pady=(self.px(16), 0)
        )
        self._chips(NOTIFY, "%s", dot=False)
        row = tk.Frame(self.inner, bg=self.c["bg"])
        row.pack(fill="x", pady=(self.px(10), 0))
        self._link(row, "Open data folder", self.tray.open_data_folder).pack(
            side="left"
        )
        self._link(row, "Quit agentwatch", self._quit).pack(side="right")

    def _quit(self):
        """Close Tk here, on its own thread, then stop the tray elsewhere --
        stopping waits for this thread to finish."""
        self.hide()
        self.root.destroy()
        threading.Thread(target=self.tray.quit, daemon=False).start()
