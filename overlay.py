"""
Cinematic on-screen overlay drawn directly on top of the real desktop.

A fullscreen, transparent, always-on-top, click-through window that renders:
  * a glowing "REMOTE CONTROL ENGAGED" frame while Jarvis is driving the PC
  * a sweeping scan line whenever Jarvis "looks" at the screen
  * an animated targeting reticle wherever Jarvis is about to click

Windows draws it with Tk; Linux with GTK + cairo as an X11 window (also under Wayland, through
XWayland, since only X11 windows can float above everything and let clicks through).
All GUI work happens on one dedicated thread; other threads talk to it via a queue.
"""
from __future__ import annotations

import contextlib
import ctypes
import math
import os
import queue
import sys
import threading
import time

TRANSP = "#010203"          # colour key that Windows renders as fully transparent
CYAN = "#00e5ff"
CYAN_DIM = "#00788a"
CYAN_DARK = "#003a44"
ORANGE = "#ff9d1c"
WHITE = "#e8fdff"
K = 1.0                     # UI scale (screen height / 1080), set at runtime


def F(size):
    return ("Consolas", size, "bold")   # Tk already scales points by DPI (cairo backend: see _Cairo)


def _ease(p: float) -> float:
    p = max(0.0, min(1.0, p))
    return 1 - (1 - p) ** 3


class Overlay:
    def __init__(self, screen_size: tuple[int, int] | None = None, cursor=None) -> None:
        """screen_size: the coordinate space of target() points (the mouse-control screen).
        cursor: callable returning the pointer position in that space (for the trail)."""
        self.q: queue.Queue = queue.Queue()
        self.enabled = False
        self._screen = screen_size
        self._cursor = cursor
        self._ready = threading.Event()
        run = self._run_tk if sys.platform == "win32" else self._run_gtk
        threading.Thread(target=run, name="jarvis-overlay", daemon=True).start()
        self._ready.wait(3)

    # ------------------------------------------------------------------ public API
    def control(self, on: bool) -> None:
        self.q.put(("control", on))

    def scan(self) -> None:
        self.q.put(("scan",))

    def target(self, x: int, y: int, label: str = "TARGET") -> None:
        self.q.put(("target", x, y, label))

    def trail(self, on: bool) -> None:
        self.q.put(("trail", on))

    @staticmethod
    def set_theme(primary: str, secondary: str | None = None) -> None:
        """Recolor the desktop overlay (drawing reads these module globals every frame)."""
        global CYAN, CYAN_DIM, CYAN_DARK, ORANGE

        def shade(hex_, k):
            r, g, b = (int(hex_[i:i + 2], 16) for i in (1, 3, 5))
            r, g, b = (max(1, min(255, int(v * k))) for v in (r, g, b))
            return f"#{r:02x}{g:02x}{b:02x}"
        CYAN, CYAN_DIM, CYAN_DARK = primary, shade(primary, 0.53), shade(primary, 0.25)
        if secondary:
            ORANGE = secondary

    @contextlib.contextmanager
    def hidden(self):
        """Temporarily hide everything (used while capturing the screen)."""
        if not self.enabled:
            yield
            return
        ev = threading.Event()
        self.q.put(("hide", ev))
        ev.wait(0.4)
        time.sleep(0.03)
        try:
            yield
        finally:
            self.q.put(("unhide",))

    # ------------------------------------------------------------------ engine (shared by Tk and GTK)
    @staticmethod
    def _new_state(scale=(1.0, 1.0)) -> dict:
        return {
            "control": False, "control_t0": 0.0,
            "scan_t0": None, "target": None, "hidden": False,
            "hide_ev": None, "trail": False, "trail_pts": [], "dirty": True, "scale": scale,
        }

    def _step(self, st: dict, now: float) -> bool:
        """Apply queued commands. Returns True when the overlay must be redrawn."""
        sx, sy = st["scale"]
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                st["dirty"] = True
                if kind == "control":
                    st["control"] = msg[1]
                    st["control_t0"] = now
                    if not msg[1]:
                        st["trail_pts"].clear()
                elif kind == "scan":
                    st["scan_t0"] = now
                elif kind == "target":
                    st["target"] = (msg[1] * sx, msg[2] * sy, msg[3], now)
                elif kind == "trail":
                    st["trail"] = msg[1]
                    st["trail_pts"].clear()
                elif kind == "hide":
                    st["hidden"] = True
                    st["hide_ev"] = msg[1]
                elif kind == "unhide":
                    st["hidden"] = False
        except queue.Empty:
            pass
        active = (st["control"] or st["scan_t0"] or st["target"]) and not st["hidden"]
        return bool(active or st["dirty"])

    def _paint(self, cv, st: dict, sw: int, sh: int, now: float) -> None:
        if not st["hidden"]:
            if st["control"]:
                self._draw_frame(cv, sw, sh, now - st["control_t0"])
                if st["trail"]:
                    c = self._cursor() if self._cursor else None
                    if c:
                        st["trail_pts"].append((c[0] * st["scale"][0], c[1] * st["scale"][1]))
                        st["trail_pts"][:] = st["trail_pts"][-40:]
                    self._draw_trail(cv, st["trail_pts"])
            if st["scan_t0"]:
                if not self._draw_scan(cv, sw, sh, now - st["scan_t0"]):
                    st["scan_t0"] = None
            if st["target"]:
                x, y, label, t0 = st["target"]
                if not self._draw_reticle(cv, x, y, label, now - t0):
                    st["target"] = None
        st["dirty"] = False

    # ------------------------------------------------------------------ Windows: Tk thread
    def _run_tk(self) -> None:
        try:
            import tkinter as tk
        except Exception as exc:  # pragma: no cover
            print(f"[overlay] disabled: {exc}")
            self._ready.set()
            return
        try:
            root = tk.Tk()
            root.overrideredirect(True)
            sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
            global K
            K = max(1.0, sh / 1080)
            root.geometry(f"{sw}x{sh}+0+0")
            root.configure(bg=TRANSP)
            root.attributes("-topmost", True)
            root.attributes("-transparentcolor", TRANSP)
            cv = tk.Canvas(root, width=sw, height=sh, bg=TRANSP, highlightthickness=0)
            cv.pack()
            root.update_idletasks()
            self._make_click_through(root)
        except Exception as exc:
            print(f"[overlay] disabled: {exc}")
            self._ready.set()
            return

        st = self._new_state()
        self.enabled = True
        self._ready.set()

        def frame():
            now = time.time()
            if self._step(st, now):
                cv.delete("all")
                self._paint(cv, st, sw, sh, now)
                if st["hidden"] and st["hide_ev"] is not None:
                    root.update_idletasks()
                    st["hide_ev"].set()
                    st["hide_ev"] = None
            root.after(16, frame)

        root.after(16, frame)
        root.mainloop()

    # ------------------------------------------------------------------ Linux: GTK thread
    def _run_gtk(self) -> None:
        try:
            import gi
            gi.require_version("Gtk", "3.0")
            gi.require_version("Gdk", "3.0")
            # Must be X11: Wayland has no always-on-top, click-through windows for normal apps.
            # Set only for this import, so apps Jarvis launches don't inherit it.
            old = os.environ.get("GDK_BACKEND")
            os.environ["GDK_BACKEND"] = "x11"
            try:
                from gi.repository import Gdk, GLib, Gtk
            finally:
                if old is None:
                    os.environ.pop("GDK_BACKEND", None)
                else:
                    os.environ["GDK_BACKEND"] = old
            import cairo
            gi.require_foreign("cairo")    # GTK<->cairo bridge (Ubuntu package python3-gi-cairo)
            if Gdk.Display.get_default() is None:
                raise RuntimeError("no X11 display")
            screen = Gdk.Screen.get_default()
            visual = screen.get_rgba_visual()
            if visual is None or not screen.is_composited():
                raise RuntimeError("the desktop has no compositor, so a transparent overlay is impossible")
            display = Gdk.Display.get_default()
            mon = display.get_primary_monitor() or display.get_monitor(0)
            geo = mon.get_geometry()
            sw, sh = geo.width, geo.height
            global K
            K = max(1.0, sh / 1080)
            win = Gtk.Window(type=Gtk.WindowType.POPUP)       # override-redirect: above everything
            win.set_visual(visual)
            win.set_app_paintable(True)
            win.set_accept_focus(False)
            win.move(geo.x, geo.y)
            win.resize(sw, sh)
        except Exception as exc:
            print(f"[overlay] disabled: {exc}")
            self._ready.set()
            return

        scr = self._screen or (sw, sh)
        st = self._new_state((sw / scr[0], sh / scr[1]))

        def on_draw(_w, cr):
            cr.set_operator(cairo.OPERATOR_SOURCE)
            cr.set_source_rgba(0, 0, 0, 0)
            cr.paint()
            cr.set_operator(cairo.OPERATOR_OVER)
            self._paint(_Cairo(cr), st, sw, sh, time.time())
            if st["hidden"] and st["hide_ev"] is not None:
                ev, st["hide_ev"] = st["hide_ev"], None
                GLib.timeout_add(40, lambda: ev.set() and False)   # let the cleared frame reach the screen
            return True

        def tick():
            if self._step(st, time.time()):
                win.queue_draw()
            return True

        win.connect("draw", on_draw)
        win.show_all()
        win.input_shape_combine_region(cairo.Region())         # empty input area = click-through
        self.enabled = True
        self._ready.set()
        GLib.timeout_add(16, tick)
        Gtk.main()

    @staticmethod
    def _make_click_through(root) -> None:
        try:
            user32 = ctypes.windll.user32
            hwnd = user32.GetParent(root.winfo_id()) or root.winfo_id()
            GWL_EXSTYLE = -20
            WS_EX_LAYERED, WS_EX_TRANSPARENT = 0x80000, 0x20
            WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x80, 0x08000000
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(
                hwnd, GWL_EXSTYLE,
                style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
            )
        except Exception as exc:
            print(f"[overlay] click-through failed: {exc}")

    # ------------------------------------------------------------------ drawing
    @staticmethod
    def _draw_frame(cv, sw, sh, t) -> None:
        grow = _ease(t / 0.6)
        pulse = 0.5 + 0.5 * math.sin(t * 4)
        col = CYAN if pulse > 0.35 else CYAN_DIM
        m = int(4 * K)
        cv.create_rectangle(m, m, sw - m, sh - m, outline=CYAN_DARK, width=int(2 * K))
        L = int(140 * K * grow)
        for (x, y, dx, dy) in ((m, m, 1, 1), (sw - m, m, -1, 1), (m, sh - m, 1, -1), (sw - m, sh - m, -1, -1)):
            cv.create_line(x, y, x + dx * L, y, fill=col, width=int(6 * K))
            cv.create_line(x, y, x, y + dy * L, fill=col, width=int(6 * K))
        # top banner
        w = int(560 * K * grow)
        cx = sw // 2
        if w > 40:
            cv.create_polygon(cx - w // 2, 0, cx + w // 2, 0, cx + w // 2 - 26 * K, 40 * K, cx - w // 2 + 26 * K, 40 * K,
                              fill="#021820", outline=col, width=2)
            if grow > 0.9:
                cv.create_text(cx, 20 * K, text="J.A.R.V.I.S  //  REMOTE CONTROL ENGAGED",
                               fill=WHITE if pulse > 0.5 else CYAN, font=F(13))
        # side ticks
        for i in range(12):
            yy = sh * 0.3 + i * 18 * K
            ln = (18 if i % 3 == 0 else 9) * K
            cv.create_line(m + 10 * K, yy, m + 10 * K + ln, yy, fill=CYAN_DIM, width=int(2 * K))
            cv.create_line(sw - m - 10 * K, yy, sw - m - 10 * K - ln, yy, fill=CYAN_DIM, width=int(2 * K))

    @staticmethod
    def _draw_trail(cv, pts) -> None:
        n = len(pts)
        for i in range(1, n):
            col = CYAN if i > n * 0.66 else (CYAN_DIM if i > n * 0.33 else CYAN_DARK)
            cv.create_line(*pts[i - 1], *pts[i], fill=col, width=int(3 * K))

    @staticmethod
    def _draw_scan(cv, sw, sh, t) -> bool:
        dur = 0.9
        if t > dur:
            return False
        y = int(sh * _ease(t / dur))
        for off, col, w in ((0, CYAN, 3), (10, CYAN_DIM, 2), (26, CYAN_DARK, 2), (50, CYAN_DARK, 1)):
            cv.create_line(0, y - off * K, sw, y - off * K, fill=col, width=int(w * K))
        cv.create_text(sw - 20 * K, max(y - 14 * K, 14 * K), text=f"SCANNING  {int(100 * t / dur):3d}%",
                       fill=CYAN, anchor="e", font=F(11))
        return True

    @staticmethod
    def _draw_reticle(cv, x, y, label, t) -> bool:
        dur = 1.6
        if t > dur:
            return False
        p = _ease(t / 0.75)
        locked = t > 0.75
        col = ORANGE if locked else CYAN
        r = (110 - 72 * p) * K
        rot = t * 300
        for k in range(4):
            cv.create_arc(x - r, y - r, x + r, y + r, start=rot + k * 90, extent=55,
                          style="arc", outline=col, width=int(3 * K))
        r2 = r + 16 * K
        for k in range(3):
            cv.create_arc(x - r2, y - r2, x + r2, y + r2, start=-rot * 0.6 + k * 120, extent=30,
                          style="arc", outline=CYAN_DIM, width=int(2 * K))
        g = 10 * K + r * 0.4
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            cv.create_line(x + dx * g, y + dy * g, x + dx * (g + 18 * K), y + dy * (g + 18 * K), fill=col, width=int(2 * K))
        cv.create_oval(x - 3 * K, y - 3 * K, x + 3 * K, y + 3 * K, fill=col, outline=col)
        if locked:
            cv.create_text(x + r2 + 12 * K, y - 13 * K, text="TARGET LOCKED", anchor="w",
                           fill=ORANGE, font=F(12))
        cv.create_text(x + r2 + 12 * K, y + 13 * K, text=label[:40].upper(), anchor="w",
                       fill=WHITE, font=F(10))
        return True


class _Cairo:
    """The few Tk-canvas drawing calls used above, drawn with cairo (so the drawing code is shared)."""

    def __init__(self, cr):
        self.cr = cr
        cr.set_line_cap(1)   # round caps, like Tk's smooth look
        cr.select_font_face("monospace", 0, 1)   # normal slant, bold

    def _rgb(self, col):
        self.cr.set_source_rgb(*(int(col[i:i + 2], 16) / 255 for i in (1, 3, 5)))

    def _stroke(self, col, width):
        self._rgb(col)
        self.cr.set_line_width(max(1, width))
        self.cr.stroke()

    def create_line(self, x0, y0, x1, y1, fill, width=1):
        self.cr.move_to(x0, y0)
        self.cr.line_to(x1, y1)
        self._stroke(fill, width)

    def create_rectangle(self, x0, y0, x1, y1, outline, width=1):
        self.cr.rectangle(x0, y0, x1 - x0, y1 - y0)
        self._stroke(outline, width)

    def create_polygon(self, *pts, fill, outline, width=1):
        self.cr.move_to(pts[0], pts[1])
        for i in range(2, len(pts), 2):
            self.cr.line_to(pts[i], pts[i + 1])
        self.cr.close_path()
        self._rgb(fill)
        self.cr.fill_preserve()
        self._stroke(outline, width)

    def create_oval(self, x0, y0, x1, y1, fill, outline):
        self.cr.arc((x0 + x1) / 2, (y0 + y1) / 2, (x1 - x0) / 2, 0, 2 * math.pi)
        self._rgb(fill)
        self.cr.fill()

    def create_arc(self, x0, y0, x1, y1, start, extent, style, outline, width=1):
        # Tk: degrees, counter-clockwise from 3 o'clock. cairo: radians, clockwise (y points down).
        self.cr.new_sub_path()
        self.cr.arc((x0 + x1) / 2, (y0 + y1) / 2, (x1 - x0) / 2,
                    math.radians(-(start + extent)), math.radians(-start))
        self._stroke(outline, width)

    def create_text(self, x, y, text, fill, font, anchor="center"):
        self.cr.set_font_size(font[1] * 1.33 * K)   # points -> pixels, scaled like the rest
        ext = self.cr.text_extents(text)
        tx = x - ext.x_bearing - {"e": ext.width, "w": 0}.get(anchor, ext.width / 2)
        ty = y - ext.y_bearing - ext.height / 2
        self.cr.move_to(tx, ty)
        self._rgb(fill)
        self.cr.show_text(text)
