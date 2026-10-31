"""Multi-Blu's window.

One list. Flip the switch next to a pair of headphones and they start hearing
whatever this machine is playing. Nothing else to set up.

Every fixed pixel size goes through px(), because Tk sizes fonts in points (which
follow the display's DPI) but takes widget dimensions in raw pixels (which do
not). Without that the layout falls apart on a scaled display.
"""

from __future__ import annotations

import ctypes
import json
import os
import queue
import threading
import tkinter as tk
from pathlib import Path

from . import endpoints as ep
from .engine import Engine

BG = "#0B0C0F"
CARD = "#15171C"
HOVER = "#1C2027"
EDGE = "#22252D"
TEXT = "#EDEEF2"
DIM = "#767D8B"
FAINT = "#4A505C"
ACCENT = "#3ED598"
TRACK = "#2A2E37"
KNOB = "#FFFFFF"
DANGER = "#F2777A"

UI = "Segoe UI"
SETTINGS = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Multi-Blu" / "settings.json"
SCAN_MS = 1500
MAX_DELAY = 0.30        # seconds of hand-tuned sync nudge per device

SCALE = 1.0


def px(value: float) -> int:
    return max(1, round(value * SCALE))


def _enable_dpi() -> None:
    """Render at the display's real pixel density instead of being upscaled."""
    global SCALE
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    try:
        SCALE = ctypes.windll.user32.GetDpiForSystem() / 96.0
    except Exception:
        SCALE = 1.0


def _colorref(hex_colour: str) -> int:
    """#RRGGBB -> Win32 COLORREF, which orders the bytes 0x00BBGGRR."""
    r, g, b = (int(hex_colour[i:i + 2], 16) for i in (1, 3, 5))
    return (b << 16) | (g << 8) | r


def _dark_titlebar(window) -> None:
    """Paint the title bar to match the window.

    Dark mode alone is not enough: with "show accent colour on title bars" on,
    Windows repaints the caption in the user's accent colour, so the caption and
    border colours are set explicitly too (Windows 11 22000+).
    """
    try:
        window.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        dwm = ctypes.windll.dwmapi

        def apply(attribute, value):
            data = ctypes.c_int(value)
            return dwm.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(data),
                                             ctypes.sizeof(data))

        for attribute in (20, 19):      # DWMWA_USE_IMMERSIVE_DARK_MODE, then legacy
            if apply(attribute, 1) == 0:
                break
        apply(34, _colorref(EDGE))      # DWMWA_BORDER_COLOR
        apply(35, _colorref(BG))        # DWMWA_CAPTION_COLOR
        apply(36, _colorref(TEXT))      # DWMWA_TEXT_COLOR
    except Exception:
        pass


def _blend(a: str, b: str, t: float) -> str:
    t = max(0.0, min(1.0, t))
    ar, ag, ab = int(a[1:3], 16), int(a[3:5], 16), int(a[5:7], 16)
    br, bg_, bb = int(b[1:3], 16), int(b[3:5], 16), int(b[5:7], 16)
    return "#%02x%02x%02x" % (
        round(ar + (br - ar) * t),
        round(ag + (bg_ - ag) * t),
        round(ab + (bb - ab) * t),
    )


class Toggle(tk.Canvas):
    """A pill switch that animates between off and on."""

    def __init__(self, master, command=None, bg=CARD):
        self.w, self.h, self.pad = px(44), px(24), px(3)
        super().__init__(master, width=self.w, height=self.h, bg=bg,
                         highlightthickness=0, bd=0, cursor="hand2")
        self.command = command
        self.on = False
        self._t = 0.0
        self._job = None
        self.bind("<Button-1>", self._clicked)
        self._render()

    def set(self, on: bool, animate: bool = True) -> None:
        if on == self.on:
            return
        self.on = on
        if animate:
            self._animate()
        else:
            self._t = 1.0 if on else 0.0
            self._render()

    def _clicked(self, _event) -> None:
        self.set(not self.on)
        if self.command:
            self.command(self.on)

    def _animate(self) -> None:
        if self._job:
            self.after_cancel(self._job)
        target = 1.0 if self.on else 0.0
        step = 0.18 if self.on else -0.18

        def tick():
            self._t = max(0.0, min(1.0, self._t + step))
            self._render()
            self._job = self.after(12, tick) if self._t != target else None

        tick()

    def _render(self) -> None:
        self.delete("all")
        r = (self.h - 2 * self.pad) / 2
        colour = _blend(TRACK, ACCENT, self._t)
        y0, y1 = self.pad, self.h - self.pad
        self.create_oval(self.pad, y0, self.pad + 2 * r, y1, fill=colour, outline="")
        self.create_oval(self.w - self.pad - 2 * r, y0, self.w - self.pad, y1,
                         fill=colour, outline="")
        self.create_rectangle(self.pad + r, y0, self.w - self.pad - r, y1,
                              fill=colour, outline="")
        kr = r - px(2)
        cx = self.pad + r + self._t * (self.w - 2 * self.pad - 2 * r)
        cy = self.h / 2
        self.create_oval(cx - kr, cy - kr, cx + kr, cy + kr, fill=KNOB, outline="")


class Slider(tk.Canvas):
    """Thin volume slider, grabbed anywhere along its length."""

    def __init__(self, master, command=None, bg=CARD):
        self.h, self.knob = px(20), px(6)
        super().__init__(master, width=px(150), height=self.h, bg=bg,
                         highlightthickness=0, bd=0, cursor="hand2")
        self.command = command
        self.value = 1.0
        self._span = px(150)
        self.bind("<Button-1>", self._drag)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<Configure>", self._resized)
        self._render()

    def set(self, value: float) -> None:
        self.value = max(0.0, min(1.0, value))
        self._render()

    def _resized(self, event) -> None:
        self._span = event.width
        self._render()

    def _drag(self, event) -> None:
        usable = max(1, self._span - 2 * self.knob)
        self.value = max(0.0, min(1.0, (event.x - self.knob) / usable))
        self._render()
        if self.command:
            self.command(self.value)

    def _render(self) -> None:
        self.delete("all")
        y = self.h / 2
        x0, x1 = self.knob, max(self.knob + 1, self._span - self.knob)
        self.create_line(x0, y, x1, y, fill=TRACK, width=px(4), capstyle=tk.ROUND)
        cx = x0 + self.value * (x1 - x0)
        if cx > x0:
            self.create_line(x0, y, cx, y, fill=ACCENT, width=px(4), capstyle=tk.ROUND)
        self.create_oval(cx - self.knob, y - self.knob, cx + self.knob, y + self.knob,
                         fill=KNOB, outline="")


class DeviceRow(tk.Frame):
    """One output device: name, on/off switch, and volume once it is on."""

    def __init__(self, master, app, device):
        super().__init__(master, bg=CARD, highlightbackground=EDGE, highlightthickness=1)
        self.app = app
        self.device = device

        head = tk.Frame(self, bg=CARD)
        head.pack(fill="x", padx=px(14), pady=(px(10), 0))

        labels = tk.Frame(head, bg=CARD)
        labels.pack(side="left", fill="x", expand=True)
        self.name = tk.Label(labels, text=device.name, bg=CARD, fg=TEXT,
                             font=(UI, 10), anchor="w", justify="left")
        self.name.pack(fill="x")
        self.note = tk.Label(labels, text="", bg=CARD, fg=FAINT, font=(UI, 8), anchor="w")

        self.toggle = Toggle(head, command=self._toggled)
        self.toggle.pack(side="right", padx=(px(10), 0))

        self.volume_row = tk.Frame(self, bg=CARD)
        self.slider = Slider(self.volume_row, command=self._volume)
        self.slider.pack(side="left", fill="x", expand=True)
        self.percent = tk.Label(self.volume_row, text="", bg=CARD, fg=DIM,
                                font=(UI, 8), width=4, anchor="e")
        self.percent.pack(side="right", padx=(px(8), 0))

        self.sync_row = tk.Frame(self, bg=CARD)
        tk.Label(self.sync_row, text="delay", bg=CARD, fg=FAINT, font=(UI, 8),
                 anchor="w").pack(side="left", padx=(0, px(8)))
        self.sync = Slider(self.sync_row, command=self._delay)
        self.sync.pack(side="left", fill="x", expand=True)
        self.sync_value = tk.Label(self.sync_row, text="0 ms", bg=CARD, fg=DIM,
                                   font=(UI, 8), width=6, anchor="e")
        self.sync_value.pack(side="right", padx=(px(8), 0))

        self.spacer = tk.Frame(self, bg=CARD, height=px(10))
        self.spacer.pack(fill="x")

    def _toggled(self, on: bool) -> None:
        self.app.set_selected(self.device.id, on)

    def _volume(self, value: float) -> None:
        self.percent.configure(text="%d%%" % round(value * 100))
        self.app.set_volume(self.device.id, value)

    def _delay(self, value: float) -> None:
        seconds = value * MAX_DELAY
        self.sync_value.configure(text="%d ms" % round(seconds * 1000))
        self.app.set_delay(self.device.id, seconds)

    def show_selected(self, selected: bool, animate: bool = True) -> None:
        self.toggle.set(selected, animate=animate)
        if selected:
            level = self.app.volumes.get(self.device.id, 1.0)
            self.slider.set(level)
            self.percent.configure(text="%d%%" % round(level * 100))
            seconds = self.app.delays.get(self.device.id, 0.0)
            self.sync.set(seconds / MAX_DELAY)
            self.sync_value.configure(text="%d ms" % round(seconds * 1000))
            self.volume_row.pack(fill="x", padx=px(14), pady=(px(8), 0),
                                 before=self.spacer)
            self.sync_row.pack(fill="x", padx=px(14), pady=(px(4), 0),
                               before=self.spacer)
        else:
            self.volume_row.pack_forget()
            self.sync_row.pack_forget()

    def show_note(self, text: str, bad: bool = False) -> None:
        if text:
            self.note.configure(text=text, fg=DANGER if bad else FAINT)
            self.note.pack(fill="x")
        else:
            self.note.pack_forget()


class AddDeviceButton(tk.Frame):
    """The obvious way to bring another pair of headphones into the list."""

    def __init__(self, master, command):
        super().__init__(master, bg=CARD, highlightbackground=EDGE,
                         highlightthickness=1, cursor="hand2")
        self.command = command
        inner = tk.Frame(self, bg=CARD)
        inner.pack(fill="x", padx=px(14), pady=px(9))
        plus = tk.Label(inner, text="+", bg=CARD, fg=ACCENT, font=(UI, 14, "bold"))
        plus.pack(side="left", padx=(0, px(10)))
        caption = tk.Label(inner, text="Add a Bluetooth device", bg=CARD, fg=ACCENT,
                           font=(UI, 10), anchor="w")
        caption.pack(side="left", fill="x", expand=True)

        self._parts = (self, inner, plus, caption)
        for widget in self._parts:
            widget.configure(cursor="hand2")
            widget.bind("<Button-1>", lambda e: self.command())
            widget.bind("<Enter>", lambda e: self._tint(HOVER))
            widget.bind("<Leave>", lambda e: self._tint(CARD))

    def _tint(self, colour: str) -> None:
        for widget in self._parts:
            widget.configure(bg=colour)


class Worker(threading.Thread):
    """Applies the wanted routing off the UI thread, coalescing rapid changes."""

    def __init__(self, engine: Engine, results: queue.Queue):
        super().__init__(daemon=True)
        self.engine = engine
        self.results = results
        self._wake = threading.Event()
        self._wanted = None
        self._applied = object()
        self._stopping = False

    def request(self, source: str, listeners: list) -> None:
        self._wanted = (source, tuple(listeners))
        self._wake.set()

    def shutdown(self) -> None:
        self._stopping = True
        self._wanted = None
        self._wake.set()

    def run(self) -> None:
        ep.com_init()
        while True:
            self._wake.wait()
            self._wake.clear()
            if self._stopping:
                self.engine.stop()
                return
            wanted = self._wanted
            if wanted is None or wanted == self._applied:
                continue
            self._applied = wanted
            source, listeners = wanted
            if not listeners:
                self.engine.stop()
                report = {"source": source, "ok": [], "failed": {}, "missing": []}
            else:
                report = self.engine.start(source, list(listeners))
            if self._wanted == wanted:          # nothing newer arrived meanwhile
                self.results.put(report)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        ep.com_init()
        self.title("Multi-Blu")
        self.configure(bg=BG)
        self.geometry("%dx%d" % (px(430), px(560)))
        self.minsize(px(380), px(440))

        self.engine = Engine()
        self.results: queue.Queue = queue.Queue()
        self.worker = Worker(self.engine, self.results)
        self.worker.start()

        self.devices: list = []
        self.source = None
        self.selected: set = set()
        self.delays: dict = {}
        self.volumes: dict = {}
        self.rows: dict = {}
        self._signature = None
        self._report = None
        self._scan_job = None

        self._build()
        _dark_titlebar(self)
        self._load_settings()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self._scan()
        self._drain()

    # -- layout ------------------------------------------------------------

    def _build(self) -> None:
        outer = tk.Frame(self, bg=BG)
        outer.pack(fill="both", expand=True, padx=px(18), pady=px(16))

        tk.Label(outer, text="Multi-Blu", bg=BG, fg=TEXT,
                 font=(UI, 17, "bold"), anchor="w").pack(fill="x")
        self.subtitle = tk.Label(outer, text="Play your audio on several headphones at once",
                                 bg=BG, fg=DIM, font=(UI, 9), anchor="w", justify="left")
        self.subtitle.pack(fill="x", pady=(px(2), px(14)))

        self._caption(outer, "PLAYING FROM")
        source_card = tk.Frame(outer, bg=CARD, highlightbackground=EDGE,
                               highlightthickness=1)
        source_card.pack(fill="x", pady=(px(6), px(14)))
        inner = tk.Frame(source_card, bg=CARD)
        inner.pack(fill="x", padx=px(14), pady=px(10))
        self.source_label = tk.Label(inner, text="Looking...", bg=CARD, fg=TEXT,
                                     font=(UI, 10), anchor="w")
        self.source_label.pack(side="left", fill="x", expand=True)
        self.change = tk.Label(inner, text="Change", bg=CARD, fg=ACCENT,
                               font=(UI, 9), cursor="hand2")
        self.change.pack(side="right")
        self.change.bind("<Button-1>", self._choose_source)

        self._caption(outer, "ALSO PLAYING TO")

        # Footer is packed before the list so a long list can never push the
        # add button and status off the bottom of the window.
        footer = tk.Frame(outer, bg=BG)
        footer.pack(side="bottom", fill="x", pady=(px(12), 0))
        AddDeviceButton(footer, self._open_bluetooth).pack(fill="x")
        self.status = tk.Label(footer, text="", bg=BG, fg=DIM, font=(UI, 8),
                               anchor="w", justify="left")
        self.status.pack(fill="x", pady=(px(9), 0))

        holder = tk.Frame(outer, bg=BG)
        holder.pack(fill="both", expand=True, pady=(px(6), 0))
        self.canvas = tk.Canvas(holder, bg=BG, highlightthickness=0, bd=0)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.list = tk.Frame(self.canvas, bg=BG)
        self._window = self.canvas.create_window((0, 0), window=self.list, anchor="nw")
        self.list.bind("<Configure>", lambda e: self.canvas.configure(
            scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(
            self._window, width=e.width))
        self.bind_all("<MouseWheel>", self._wheel)
        self.bind("<Configure>", self._rewrap)

    def _caption(self, parent, text: str) -> None:
        tk.Label(parent, text=text, bg=BG, fg=FAINT, font=(UI, 8, "bold"),
                 anchor="w").pack(fill="x")

    def _rewrap(self, event) -> None:
        if event.widget is self:
            width = max(px(200), event.width - px(40))
            self.subtitle.configure(wraplength=width)
            self.status.configure(wraplength=width)

    def _wheel(self, event) -> None:
        box = self.canvas.bbox("all")
        if box and box[3] > self.canvas.winfo_height():
            self.canvas.yview_scroll(-int(event.delta / 60), "units")

    # -- devices -----------------------------------------------------------

    def _scan(self) -> None:
        try:
            devices = ep.list_render_endpoints()
            default = ep.get_default_id()
        except Exception:
            devices, default = [], None

        self.devices = devices
        self.source = next((d for d in devices if d.id == default), None)
        if self.source is None and devices:
            self.source = devices[0]

        self.selected &= {d.id for d in devices}
        if self.source is not None:
            self.selected.discard(self.source.id)

        signature = (tuple(sorted((d.id, d.name) for d in devices)),
                     self.source.id if self.source else None)
        if signature != self._signature:
            self._signature = signature
            self._rebuild_rows()
            self._apply()
        self._scan_job = self.after(SCAN_MS, self._scan)

    def _rebuild_rows(self) -> None:
        self.source_label.configure(
            text=self.source.name if self.source else "No output device")

        others = [d for d in self.devices if not self.source or d.id != self.source.id]
        for row in self.rows.values():
            row.destroy()
        self.rows = {}

        if not others:
            empty = tk.Label(self.list, bg=CARD, fg=DIM, font=(UI, 9),
                             text="Nothing else connected yet.\nUse + below to pair "
                                  "another pair of headphones.",
                             justify="left", anchor="w", padx=px(14), pady=px(14))
            empty.pack(fill="x")
            self.rows["__empty__"] = empty
            return

        for device in others:
            row = DeviceRow(self.list, self, device)
            row.pack(fill="x", pady=(0, px(8)))
            row.show_selected(device.id in self.selected, animate=False)
            self.rows[device.id] = row
        self._paint_notes()

    # -- routing -----------------------------------------------------------

    def set_selected(self, device_id: str, on: bool) -> None:
        if on:
            self.selected.add(device_id)
        else:
            self.selected.discard(device_id)
        row = self.rows.get(device_id)
        if isinstance(row, DeviceRow):
            row.show_selected(on)
        self._save_settings()
        self._apply()

    def set_delay(self, device_id: str, seconds: float) -> None:
        """Shift one device in time; takes effect immediately, no restart."""
        self.delays[device_id] = seconds
        self.engine.set_delay(device_id, seconds)
        self._save_settings()

    def set_volume(self, device_id: str, level: float) -> None:
        """Change one device's own level; takes effect immediately, no restart."""
        self.volumes[device_id] = level
        self.engine.set_volume(device_id, level)
        self._save_settings()

    def _apply(self) -> None:
        if self.source is None:
            self.status.configure(text="No output device to share.", fg=DIM)
            return
        by_id = {d.id: d.name for d in self.devices}
        listeners = [(i, by_id[i]) for i in sorted(self.selected) if i in by_id]
        self.engine.delays = dict(self.delays)
        self.engine.volumes = dict(self.volumes)
        self.worker.request(self.source.name, listeners)
        if listeners:
            self.status.configure(text="Connecting...", fg=DIM)
        else:
            self.status.configure(text="Turn on anyone who should hear this too.",
                                  fg=DIM)

    def _drain(self) -> None:
        arrived = False
        try:
            while True:
                self._report = self.results.get_nowait()
                arrived = True
        except queue.Empty:
            pass
        if arrived:
            self._paint_notes()
            self._paint_status()
        self.after(120, self._drain)

    def _paint_notes(self) -> None:
        report = self._report or {}
        failed = report.get("failed", {})
        missing = set(report.get("missing", []))
        playing = set(report.get("ok", []))
        for device_id, row in self.rows.items():
            if not isinstance(row, DeviceRow):
                continue
            if device_id in failed:
                row.show_note("In use by something else", bad=True)
            elif device_id in missing:
                row.show_note("Not reachable", bad=True)
            elif device_id in self.selected and device_id in playing:
                row.show_note("Listening")
            else:
                row.show_note("")

    def _paint_status(self) -> None:
        report = self._report or {}
        if report.get("failed", {}).get("*"):
            self.status.configure(text=report["failed"]["*"], fg=DANGER)
            return
        count = len(report.get("ok", []))
        broken = len(report.get("failed", {})) + len(report.get("missing", []))
        if count and not broken:
            others = "1 other device" if count == 1 else "%d other devices" % count
            self.status.configure(
                text="Playing to %s alongside %s." % (others, report.get("source", "you")),
                fg=ACCENT)
        elif count:
            self.status.configure(
                text="Playing to %d, but %d could not be opened." % (count, broken),
                fg=DANGER)
        elif broken:
            self.status.configure(text="Could not open the selected device.", fg=DANGER)
        else:
            self.status.configure(text="Turn on anyone who should hear this too.",
                                  fg=DIM)

    # -- source picker -----------------------------------------------------

    def _choose_source(self, _event) -> None:
        menu = tk.Menu(self, tearoff=0, bg=CARD, fg=TEXT, bd=0,
                       activebackground=ACCENT, activeforeground=BG, font=(UI, 9))
        for device in self.devices:
            mark = "• " if device is self.source else "    "
            menu.add_command(label=mark + device.name,
                             command=lambda d=device: self._set_source(d))
        menu.add_separator()
        menu.add_command(label="    Open Windows sound settings",
                         command=lambda: os.startfile("ms-settings:sound"))
        menu.tk_popup(self.change.winfo_rootx(),
                      self.change.winfo_rooty() + self.change.winfo_height() + px(4))

    def _set_source(self, device) -> None:
        if not ep.set_default_id(device.id):
            self.status.configure(text="Windows would not switch output device.",
                                  fg=DANGER)
            return
        self.selected.discard(device.id)
        self._save_settings()
        self._scan_now()

    def _scan_now(self) -> None:
        self._signature = None
        if self._scan_job is not None:
            try:
                self.after_cancel(self._scan_job)
            except Exception:
                pass
            self._scan_job = None
        self._scan()

    def _open_bluetooth(self) -> None:
        try:
            os.startfile("ms-settings:bluetooth")
        except Exception:
            self.status.configure(text="Could not open Bluetooth settings.", fg=DANGER)

    # -- settings ----------------------------------------------------------

    def _load_settings(self) -> None:
        try:
            saved = json.loads(SETTINGS.read_text(encoding="utf-8-sig"))
            self.selected = set(saved.get("selected", []))
            self.delays = {k: float(v) for k, v in saved.get("delays", {}).items()}
            self.volumes = {k: float(v) for k, v in saved.get("volumes", {}).items()}
        except Exception:
            self.selected, self.delays, self.volumes = set(), {}, {}

    def _save_settings(self) -> None:
        try:
            SETTINGS.parent.mkdir(parents=True, exist_ok=True)
            SETTINGS.write_text(
                json.dumps({"selected": sorted(self.selected),
                            "delays": {k: round(v, 4)
                                       for k, v in self.delays.items() if v},
                            "volumes": {k: round(v, 4)
                                        for k, v in self.volumes.items()}}),
                encoding="utf-8")
        except Exception:
            pass

    def _close(self) -> None:
        if self._scan_job is not None:
            try:
                self.after_cancel(self._scan_job)
            except Exception:
                pass
        self.worker.shutdown()
        try:
            self.engine.stop()
        except Exception:
            pass
        self.destroy()


def main() -> None:
    _enable_dpi()
    App().mainloop()
