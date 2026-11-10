"""
Wayland backend: mouse, keyboard and screen capture through the desktop portal.

On Wayland, apps may not move the pointer, type into other windows or read the screen directly
(pyautogui and mss only reach XWayland windows). The supported route is the RemoteDesktop +
ScreenCast portal: the desktop asks once to share a screen and allow remote control, then hands
us an input channel plus a PipeWire video stream of that screen. The approval is remembered
(restore token), so later launches skip the dialog until it is revoked in the system settings.

This module is two things:
  * a pyautogui platform module (same functions as pyautogui/_pyautogui_x11.py), registered by
    install(), so pyautogui's high-level calls (tweened moveTo, hotkey, write...) work unchanged;
  * grab() -> the current screen as a PIL image.

Emergency stop: the top bar shows a remote-control indicator; stopping it ends the session and
every further mouse/keyboard call fails (reported to the agent as "ABORTED by user").
"""
from __future__ import annotations

import atexit
import os
import sys
import threading
import time
from pathlib import Path

ENABLED = sys.platform.startswith("linux") and bool(os.environ.get("WAYLAND_DISPLAY"))
TOKEN_FILE = Path(__file__).resolve().parent / ".portal_token"

_DEST, _OBJ = "org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop"
_RD, _SC = "org.freedesktop.portal.RemoteDesktop", "org.freedesktop.portal.ScreenCast"
_KEYBOARD, _POINTER, _MONITOR = 1, 2, 1
_CURSOR_HIDDEN, _CURSOR_EMBEDDED = 1, 2
_PERSIST_UNTIL_REVOKED = 2
_BUTTONS = {"left": 0x110, "right": 0x111, "middle": 0x112}   # evdev BTN_LEFT / BTN_RIGHT / BTN_MIDDLE

_portal: "_Portal | None" = None
_pos = [0, 0]   # Wayland never tells clients where the real pointer is: track where we put it


class _Portal:
    def __init__(self) -> None:
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gio, GLib, Gst
        self.Gio, self.GLib, self.Gst = Gio, GLib, Gst
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.sender = self.bus.get_unique_name()[1:].replace(".", "_")
        self.n = 0
        self.closed = False
        self._frame = (0.0, None)          # (monotonic time, Gst.Sample) of the newest frame
        self._new_frame = threading.Condition()

        _, res = self._request(_RD, "CreateSession", "", (), {"session_handle_token": ("s", "jarvis")})
        self.session = res["session_handle"]
        dev = {"types": ("u", _KEYBOARD | _POINTER), "persist_mode": ("u", _PERSIST_UNTIL_REVOKED)}
        token = TOKEN_FILE.read_text().strip() if TOKEN_FILE.exists() else ""
        if token:
            dev["restore_token"] = ("s", token)
        self._request(_RD, "SelectDevices", "o", (self.session,), dev)
        cursor_modes = self._prop(_SC, "AvailableCursorModes") or _CURSOR_EMBEDDED
        self._request(_SC, "SelectSources", "o", (self.session,), {
            "types": ("u", _MONITOR), "multiple": ("b", False),
            "cursor_mode": ("u", _CURSOR_HIDDEN if cursor_modes & _CURSOR_HIDDEN else _CURSOR_EMBEDDED)})
        print("[wayland] waiting for screen-sharing approval (choose your screen, then Share)...")
        code, res = self._request(_RD, "Start", "os", (self.session, ""), {}, timeout=300)
        if code != 0:
            raise RuntimeError("screen sharing / remote control was not allowed")
        if (res.get("devices", 0) & (_KEYBOARD | _POINTER)) != (_KEYBOARD | _POINTER):
            TOKEN_FILE.unlink(missing_ok=True)   # forget this choice, so the dialog asks again next time
            raise RuntimeError("the screen was shared, but 'Allow remote interaction' was switched off")
        if res.get("restore_token"):
            TOKEN_FILE.write_text(res["restore_token"])
            TOKEN_FILE.chmod(0o600)
        self.node, props = res["streams"][0]
        self._start_video()
        # Screen space = stream pixels (physical, e.g. 1920x1200 at 125% scaling), as on Windows.
        self.size = self.grab().size
        self.logical = tuple(props.get("size") or self.size)          # e.g. 1536x960
        self.scale = self.size[0] / self.logical[0]
        print(f"[wayland] remote control ready: screen {self.size[0]}x{self.size[1]}")

    def move(self, x: float, y: float) -> None:
        """Put the pointer at stream pixel (x, y).
        GNOME quirk with fractional scaling: absolute positions are checked against the LOGICAL size but
        then read as physical pixels, so only the top-left 1/scale of the screen is reachable that way.
        So: jump absolutely as far as allowed, then move the rest relatively (exact, in logical pixels)."""
        ax, ay = min(x, self.logical[0] - 1), min(y, self.logical[1] - 1)
        self.notify("NotifyPointerMotionAbsolute", "udd", self.node, float(ax), float(ay))
        if (ax, ay) != (x, y):
            self.notify("NotifyPointerMotion", "dd", (x - ax) / self.scale, (y - ay) / self.scale)

    # ------------------------------------------------------------------ D-Bus plumbing
    def _variant(self, sig: str, args: tuple, options: dict):
        V = self.GLib.Variant
        opts = {k: V(t, v) for k, (t, v) in options.items()}
        return V(f"({sig}a{{sv}})", (*args, opts))

    def _request(self, iface, method, sig, args, options, timeout=30):
        """Call a portal method that answers through a Request object and wait for its Response."""
        GLib, Gio = self.GLib, self.Gio
        self.n += 1
        token = f"jarvis{os.getpid()}_{self.n}"
        options = {**options, "handle_token": ("s", token)}
        path = f"{_OBJ}/request/{self.sender}/{token}"
        ctx = GLib.MainContext.new()
        ctx.push_thread_default()           # the Response signal is delivered to this context
        got = []
        sub = self.bus.signal_subscribe(_DEST, "org.freedesktop.portal.Request", "Response", path, None,
                                        Gio.DBusSignalFlags.NONE, lambda *a: got.append(a[5].unpack()))
        tick = GLib.timeout_source_new(200)  # wake up regularly to check the deadline
        tick.set_callback(lambda *_: True)
        tick.attach(ctx)
        try:
            self.bus.call_sync(_DEST, _OBJ, iface, method, self._variant(sig, args, options),
                               GLib.VariantType("(o)"), Gio.DBusCallFlags.NONE, -1, None)
            deadline = time.monotonic() + timeout
            while not got and time.monotonic() < deadline:
                ctx.iteration(True)
        finally:
            tick.destroy()
            self.bus.signal_unsubscribe(sub)
            ctx.pop_thread_default()
        if not got:
            raise RuntimeError(f"no answer from the desktop portal ({method})")
        code, results = got[0]
        if code != 0 and method != "Start":
            raise RuntimeError(f"desktop portal refused {method} (code {code})")
        return code, results

    def _prop(self, iface, name):
        try:
            out = self.bus.call_sync(_DEST, _OBJ, "org.freedesktop.DBus.Properties", "Get",
                                     self.GLib.Variant("(ss)", (iface, name)), None,
                                     self.Gio.DBusCallFlags.NONE, -1, None)
            return out.unpack()[0]
        except Exception:
            return None

    def notify(self, method: str, sig: str, *args) -> None:
        """Send one input event. Fails with FailSafeException once the user stopped remote control."""
        if not self.closed:
            try:
                self.bus.call_sync(_DEST, _OBJ, _RD, method,
                                   self.GLib.Variant(f"(oa{{sv}}{sig})", (self.session, {}, *args)),
                                   None, self.Gio.DBusCallFlags.NONE, -1, None)
                return
            except self.GLib.Error as exc:
                print(f"[wayland] remote control ended: {exc.message}", flush=True)
                self.closed = True
        import pyautogui
        raise pyautogui.FailSafeException("remote control was stopped from the desktop's top bar")

    # ------------------------------------------------------------------ screen frames
    def _start_video(self) -> None:
        Gio, GLib, Gst = self.Gio, self.GLib, self.Gst
        out, fds = self.bus.call_with_unix_fd_list_sync(
            _DEST, _OBJ, _SC, "OpenPipeWireRemote", GLib.Variant("(oa{sv})", (self.session, {})),
            GLib.VariantType("(h)"), Gio.DBusCallFlags.NONE, -1, None, None)
        fd = fds.get(out.unpack()[0])
        Gst.init(None)
        # plain video/x-raw caps keep frames in normal memory (not GPU buffers) so we can read them
        self.pipe = Gst.parse_launch(
            f"pipewiresrc fd={fd} path={self.node} always-copy=true ! videoconvert ! "
            "video/x-raw,format=BGRx ! appsink name=sink emit-signals=true max-buffers=1 drop=true sync=false")
        self.pipe.get_by_name("sink").connect("new-sample", self._on_sample)
        self.pipe.set_state(Gst.State.PLAYING)
        atexit.register(self.pipe.set_state, Gst.State.NULL)   # no frame callbacks while Python shuts down

    def _on_sample(self, sink):
        sample = sink.emit("pull-sample")
        with self._new_frame:
            self._frame = (time.monotonic(), sample)
            self._new_frame.notify_all()
        return self.Gst.FlowReturn.OK

    def grab(self):
        """Newest screen frame as a PIL RGB image. The desktop only sends frames when something changes,
        so wait briefly for one newer than this call (e.g. after hiding the overlay), else use the last."""
        from PIL import Image
        Gst = self.Gst
        msg = self.pipe.get_bus().pop_filtered(Gst.MessageType.ERROR | Gst.MessageType.EOS)
        if msg or self.closed:
            self.closed = True
            raise RuntimeError("screen sharing was stopped - restart Jarvis to share the screen again")
        t0 = time.monotonic()
        with self._new_frame:
            self._new_frame.wait_for(lambda: self._frame[0] > t0, timeout=0.25)
            if self._frame[1] is None:
                self._new_frame.wait_for(lambda: self._frame[1] is not None, timeout=5)
            sample = self._frame[1]
        if sample is None:
            raise RuntimeError("no picture from the screen-sharing stream")
        caps = sample.get_caps().get_structure(0)
        w, h = caps.get_value("width"), caps.get_value("height")
        buf = sample.get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        try:
            data = bytes(info.data)
        finally:
            buf.unmap(info)
        return Image.frombuffer("RGB", (w, h), data, "raw", "BGRX", len(data) // h, 1)


# ====================================================================== public API
def install() -> None:
    """Start the portal session and make pyautogui use it. Call before the first `import pyautogui`."""
    global _portal
    try:
        _portal = _Portal()
    except Exception as exc:
        sys.exit(f"\n[!] Jarvis needs permission to see and control the screen on Wayland: {exc}.\n"
                 "    Start Jarvis again: in the dialog pick your screen, switch ON 'Allow remote interaction',\n"
                 "    then click 'Share'.\n")
    _pos[:] = [_portal.size[0] // 2, _portal.size[1] // 2]
    sys.modules["mouseinfo"] = None   # its import connects to X; pyautogui treats None as "not installed"
    sys.modules["pyautogui._pyautogui_x11"] = sys.modules[__name__]


def grab():
    return _portal.grab()


# ====================================================================== pyautogui platform module
def _keysym_table() -> dict:
    import Xlib.XK
    Xlib.XK.load_keysym_group("xf86")
    names = {
        "backspace": "BackSpace", "\b": "BackSpace", "tab": "Tab", "\t": "Tab", "enter": "Return",
        "return": "Return", "\n": "Return", "\r": "Return", "shift": "Shift_L", "shiftleft": "Shift_L",
        "shiftright": "Shift_R", "ctrl": "Control_L", "ctrlleft": "Control_L", "ctrlright": "Control_R",
        "alt": "Alt_L", "altleft": "Alt_L", "altright": "Alt_R", "win": "Super_L", "winleft": "Super_L",
        "winright": "Super_R", "command": "Super_L", "option": "Alt_L", "pause": "Pause",
        "capslock": "Caps_Lock", "numlock": "Num_Lock", "scrolllock": "Scroll_Lock", "esc": "Escape",
        "escape": "Escape", "space": "space", " ": "space", "pgup": "Page_Up", "pageup": "Page_Up",
        "pgdn": "Page_Down", "pagedown": "Page_Down", "end": "End", "home": "Home", "left": "Left",
        "up": "Up", "right": "Right", "down": "Down", "insert": "Insert", "del": "Delete",
        "delete": "Delete", "print": "Print", "prtsc": "Print", "prtscr": "Print", "prntscrn": "Print",
        "printscreen": "Print", "apps": "Menu", "help": "Help", "select": "Select", "execute": "Execute",
        "volumeup": "XF86AudioRaiseVolume", "volumedown": "XF86AudioLowerVolume",
        "volumemute": "XF86AudioMute", "playpause": "XF86AudioPlay", "nexttrack": "XF86AudioNext",
        "prevtrack": "XF86AudioPrev", "stop": "XF86AudioStop",
        **{f"f{i}": f"F{i}" for i in range(1, 25)}, **{f"num{i}": f"KP_{i}" for i in range(10)},
        "add": "KP_Add", "subtract": "KP_Subtract", "multiply": "KP_Multiply", "divide": "KP_Divide",
        "decimal": "KP_Decimal", "separator": "KP_Separator",
    }
    return {k: Xlib.XK.string_to_keysym(v) for k, v in names.items()}


keyboardMapping = _keysym_table() if ENABLED else {}   # Xlib (for keysym names) exists only on Linux


def _keysym(key: str) -> int | None:
    if key in keyboardMapping:
        return keyboardMapping[key]
    if len(key) == 1:   # a character: Latin-1 keysyms equal the code point, the rest are 0x01000000 + code point
        c = ord(key)
        return c if 0x20 <= c <= 0x7E or 0xA0 <= c <= 0xFF else 0x01000000 | c
    return None


def _keyDown(key):
    sym = _keysym(key)
    if sym:
        _portal.notify("NotifyKeyboardKeysym", "iu", sym, 1)


def _keyUp(key):
    sym = _keysym(key)
    if sym:
        _portal.notify("NotifyKeyboardKeysym", "iu", sym, 0)


def _position():
    return tuple(_pos)


def _size():
    return _portal.size


def _moveTo(x, y):
    if x is None or y is None:
        return
    _portal.move(x, y)
    _pos[:] = [int(x), int(y)]


def _button(x, y, button, state):
    _moveTo(x, y)
    _portal.notify("NotifyPointerButton", "iu", _BUTTONS[button], state)


def _mouseDown(x, y, button):
    _button(x, y, button, 1)


def _mouseUp(x, y, button):
    _button(x, y, button, 0)


def _click(x, y, button):
    _mouseDown(x, y, button)
    _mouseUp(x, y, button)


def _mouse_is_swapped():
    return False


def _vscroll(clicks, x=None, y=None):
    _moveTo(x, y)
    if clicks:   # pyautogui: positive = up; the portal: positive = down
        _portal.notify("NotifyPointerAxisDiscrete", "ui", 0, -int(clicks))


def _hscroll(clicks, x=None, y=None):
    _moveTo(x, y)
    if clicks:
        _portal.notify("NotifyPointerAxisDiscrete", "ui", 1, int(clicks))


_scroll = _vscroll
