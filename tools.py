"""
J.A.R.V.I.S. PC-control toolkit.

Every function in TOOLS is exposed to Gemini through function calling.
Each tool runs in a worker thread and receives a `ctx` with:
    ctx.overlay   -> on-screen cinematic overlay
    ctx.brain     -> Gemini vision helpers (locate / describe)
    ctx.emit(msg) -> push an event to the HUD (thread-safe)
"""
from __future__ import annotations

import base64
import ctypes
import datetime
import io
import json
import math
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

import wayland

IS_WINDOWS = sys.platform == "win32"
IS_WAYLAND = wayland.ENABLED
IS_X11 = sys.platform.startswith("linux") and not IS_WAYLAND
SYSTEM_DISK = os.environ.get("SystemDrive", "C:") + "\\" if IS_WINDOWS else "/"

# Make coordinates match physical pixels on scaled (125%/150%) displays.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

if IS_WAYLAND:
    wayland.install()   # GNOME/KDE ask once to share the screen; pyautogui then drives the portal

import mss
import psutil
import pyautogui
import pyperclip
from PIL import Image

pyautogui.FAILSAFE = True      # slam the mouse into a screen corner to abort
pyautogui.PAUSE = 0.04

SCREEN_W, SCREEN_H = pyautogui.size()


# ============================================================== helpers
def grab_screen(max_w: int = 1600) -> tuple[bytes, Image.Image]:
    """Capture the primary monitor -> (jpeg bytes, PIL image at capture size)."""
    if IS_WAYLAND:
        img = wayland.grab()
    else:
        with mss.MSS() as s:
            mon = next((m for m in s.monitors[1:] if m["left"] == 0 and m["top"] == 0), s.monitors[1])
            raw = s.grab(mon)
        img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
    if img.width > max_w:
        img = img.resize((max_w, int(img.height * max_w / img.width)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=82)
    return buf.getvalue(), img


def glide_to(x: int, y: int, duration: float = 0.7) -> None:
    x = max(2, min(SCREEN_W - 3, int(x)))
    y = max(2, min(SCREEN_H - 3, int(y)))
    pyautogui.moveTo(x, y, duration=duration, tween=pyautogui.easeInOutQuad)


def _typewrite(text: str) -> None:
    if text.isascii() and len(text) <= 400:
        pyautogui.write(text, interval=0.012)
    else:  # unicode / long text -> paste
        old = None
        try:
            old = pyperclip.paste()
        except Exception:
            pass
        pyperclip.copy(text)
        pyautogui.hotkey("ctrl", "v")
        time.sleep(0.15)
        if old is not None:
            try:
                pyperclip.copy(old)
            except Exception:
                pass


def _detached(cmd: list[str], **kw) -> None:
    """Start a program without tying it to Jarvis (no console window on Windows, own session on Linux)."""
    if IS_WINDOWS:
        subprocess.Popen(cmd, creationflags=subprocess.CREATE_NO_WINDOW, **kw)
    else:
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, **kw)


def _fetch(url: str, timeout: float = 8) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 JarvisHUD"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


# ============================================================== tools
KNOWN_APPS = {
    "notepad": "notepad", "calculator": "calc", "calc": "calc", "paint": "mspaint",
    "mspaint": "mspaint", "file explorer": "explorer", "explorer": "explorer",
    "files": "explorer", "command prompt": "cmd", "cmd": "cmd", "terminal": "wt",
    "powershell": "powershell", "task manager": "taskmgr", "settings": "ms-settings:",
    "camera": "microsoft.windows.camera:", "clock": "ms-clock:", "store": "ms-windows-store:",
    "snipping tool": "ms-screenclip:", "spotify": "spotify:", "calendar": "outlookcal:",
    "maps": "bingmaps:", "weather": "bingweather:", "photos": "ms-photos:",
}
# Linux: common names -> desktop-file ids to try (first installed one wins)
LINUX_APPS = {
    "notepad": ["org.gnome.TextEditor", "org.gnome.gedit", "org.kde.kate"],
    "text editor": ["org.gnome.TextEditor", "org.gnome.gedit", "org.kde.kate"],
    "calculator": ["org.gnome.Calculator", "org.kde.kcalc"], "calc": ["org.gnome.Calculator", "org.kde.kcalc"],
    "paint": ["com.github.maoschanz.drawing", "pinta", "com.github.PintaProject.Pinta", "org.kde.kolourpaint"],
    "files": ["org.gnome.Nautilus", "org.kde.dolphin"], "file explorer": ["org.gnome.Nautilus", "org.kde.dolphin"],
    "explorer": ["org.gnome.Nautilus", "org.kde.dolphin"],
    "terminal": ["org.gnome.Ptyxis", "org.gnome.Terminal", "org.gnome.Console", "org.kde.konsole"],
    "task manager": ["org.gnome.SystemMonitor", "gnome-system-monitor", "org.kde.plasma-systemmonitor"],
    "system monitor": ["org.gnome.SystemMonitor", "gnome-system-monitor", "org.kde.plasma-systemmonitor"],
    "settings": ["org.gnome.Settings", "gnome-control-center", "systemsettings"],
    "camera": ["org.gnome.Snapshot", "org.gnome.Cheese"], "clock": ["org.gnome.clocks"],
    "store": ["snap-store_snap-store", "io.snapcraft.Store", "org.gnome.Software"],
    "calendar": ["org.gnome.Calendar"], "maps": ["org.gnome.Maps"], "weather": ["org.gnome.Weather"],
    "photos": ["org.gnome.Loupe", "org.gnome.eog"], "vs code": ["code", "code_code"], "vscode": ["code", "code_code"],
    "chrome": ["google-chrome", "chromium_chromium", "chromium"], "browser": ["firefox_firefox", "firefox"],
}


def _linux_app(name: str):
    """Find an installed app (desktop file) by common name, id, display name or command."""
    from gi.repository import Gio
    key = name.strip().lower()
    apps = Gio.AppInfo.get_all()
    by_id = {(a.get_id() or "").removesuffix(".desktop").lower(): a for a in apps}
    for cand in LINUX_APPS.get(key, []):
        if cand.lower() in by_id:
            return by_id[cand.lower()]
    for a in apps:
        names = {(a.get_display_name() or "").lower(), (a.get_name() or "").lower(),
                 (a.get_id() or "").removesuffix(".desktop").lower().split(".")[-1],
                 os.path.basename(a.get_executable() or "").lower()}
        if key in names:
            return a
    return None


def open_application(ctx, name: str) -> dict:
    key = name.strip().lower()
    if IS_WINDOWS:
        target = KNOWN_APPS.get(key)
        if target:
            try:
                if target.endswith(":"):
                    os.startfile(target)
                else:
                    subprocess.Popen(["cmd", "/c", "start", "", target], shell=False,
                                     creationflags=subprocess.CREATE_NO_WINDOW)
                time.sleep(1.2)
                return {"ok": True, "opened": name, "method": "direct"}
            except Exception:
                pass
    else:
        try:
            app = _linux_app(key)
            if app and app.launch([], None):
                time.sleep(1.5)
                return {"ok": True, "opened": app.get_display_name(), "method": "direct"}
        except Exception as exc:
            print(f"[apps] {exc}")
    # Fallback: the visual way - Start menu / Activities search, exactly like a human.
    pyautogui.press("win")
    time.sleep(0.7)
    _typewrite(name)
    time.sleep(1.0)
    pyautogui.press("enter")
    time.sleep(1.8)
    return {"ok": True, "opened": name, "method": "start-menu search"}


def open_website(ctx, url: str) -> dict:
    if not re.match(r"^[a-z]+://", url):
        url = "https://" + url
    webbrowser.open(url)
    time.sleep(1.0)
    return {"ok": True, "url": url}


def google_search(ctx, query: str) -> dict:
    url = "https://www.google.com/search?q=" + urllib.parse.quote_plus(query)
    webbrowser.open(url)
    time.sleep(1.0)
    return {"ok": True, "searched": query}


def play_youtube(ctx, query: str) -> dict:
    try:
        html = _fetch("https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(query))
        ids = re.findall(r'"videoId":"([\w-]{11})"', html)
        if ids:
            webbrowser.open(f"https://www.youtube.com/watch?v={ids[0]}")
            time.sleep(1.0)
            return {"ok": True, "playing": query, "video_id": ids[0]}
    except Exception as exc:
        print("[youtube]", exc)
    webbrowser.open("https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(query))
    return {"ok": True, "note": "opened search results instead"}


def type_text(ctx, text: str, press_enter: bool = False) -> dict:
    _typewrite(text)
    if press_enter:
        pyautogui.press("enter")
    return {"ok": True, "typed_chars": len(text)}


def press_keys(ctx, keys: str) -> dict:
    parts = [k.strip().lower() for k in re.split(r"[+ ]+", keys) if k.strip()]
    alias = {"windows": "win", "control": "ctrl", "return": "enter", "escape": "esc",
             "del": "delete", "cmd": "win", "super": "win", "page down": "pagedown"}
    parts = [alias.get(p, p) for p in parts]
    bad = [p for p in parts if p not in pyautogui.KEYBOARD_KEYS]
    if bad:
        return {"ok": False, "error": f"unknown keys {bad}"}
    pyautogui.hotkey(*parts) if len(parts) > 1 else pyautogui.press(parts[0])
    time.sleep(0.3)
    return {"ok": True, "pressed": "+".join(parts)}


def click_on_screen(ctx, target: str, double_click: bool = False, right_click: bool = False) -> dict:
    ctx.overlay.scan()
    with ctx.overlay.hidden():
        jpeg, img = grab_screen()
    loc = ctx.brain.locate(jpeg, target)
    if not loc.get("found"):
        ctx.emit({"type": "vision", "image": jpeg, "point": None, "label": f"NOT FOUND: {target}"})
        return {"ok": False, "error": f"Could not find '{target}' on screen."}
    ny, nx = loc["point"]
    x, y = int(nx / 1000 * SCREEN_W), int(ny / 1000 * SCREEN_H)
    ctx.emit({"type": "vision", "image": jpeg, "point": [nx / 1000, ny / 1000], "label": target})
    ctx.overlay.target(x, y, target)
    ctx.overlay.trail(True)
    glide_to(x, y, 0.75)
    time.sleep(0.25)
    if right_click:
        pyautogui.rightClick()
    elif double_click:
        pyautogui.doubleClick()
    else:
        pyautogui.click()
    ctx.overlay.trail(False)
    time.sleep(0.5)
    return {"ok": True, "clicked": target, "at": [x, y]}


def look_at_screen(ctx, question: str = "Describe what is on the screen.") -> dict:
    ctx.overlay.scan()
    with ctx.overlay.hidden():
        jpeg, _ = grab_screen()
    ctx.emit({"type": "vision", "image": jpeg, "point": None, "label": "VISUAL ANALYSIS"})
    answer = ctx.brain.describe(jpeg, question)
    return {"ok": True, "observation": answer}


def _foreground_center():
    """Center of the active window (so scrolling hits the app the user is looking at, not the HUD).
    Wayland does not reveal other windows' positions, so there it is the screen center."""
    try:
        if IS_WINDOWS:
            import ctypes.wintypes as wt
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            r = wt.RECT()
            ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r))
            left, top, w, h = r.left, r.top, r.right - r.left, r.bottom - r.top
        elif IS_X11:
            out = subprocess.run(["xdotool", "getactivewindow", "getwindowgeometry", "--shell"],
                                 capture_output=True, text=True, timeout=2).stdout
            g = dict(line.split("=", 1) for line in out.split() if "=" in line)
            left, top, w, h = int(g["X"]), int(g["Y"]), int(g["WIDTH"]), int(g["HEIGHT"])
        else:
            raise LookupError
        if w > 200 and h > 200:
            return left + w // 2, top + h // 2
    except Exception:
        pass
    return SCREEN_W // 2, SCREEN_H // 2


def smooth_scroll(notches: int) -> None:
    """Scroll like a human: many small wheel steps instead of one jump."""
    step = -1 if notches < 0 else 1
    if IS_WINDOWS:   # Windows wheel units allow thirds of a notch
        for _ in range(abs(int(notches)) * 3):
            pyautogui.scroll(step * 40, _pause=False)
            time.sleep(0.012)
    else:            # Linux scrolls in whole notches
        for _ in range(abs(int(notches))):
            pyautogui.scroll(step, _pause=False)
            time.sleep(0.03)


def look_through_camera(ctx, question: str = "What do you see?") -> dict:
    """Webcam eyes: snapshot from the HUD's camera -> vision -> labeled boxes on the HUD feed."""
    try:
        jpeg = ctx.camera_snapshot()
    except Exception as exc:
        return {"ok": False, "error": f"camera unavailable: {exc}"}
    img64 = base64.b64encode(jpeg).decode()
    ctx.broadcast({"type": "camera_view", "image": img64, "objects": [], "label": "ANALYSING"})
    seen = ctx.brain.camera_vision(jpeg, question)
    ctx.broadcast({"type": "camera_view", "image": img64, "objects": seen["objects"], "label": question[:40]})
    return {"ok": True, "observation": seen["answer"], "objects": [o["label"] for o in seen["objects"]]}


def scroll(ctx, direction: str = "down", amount: int = 5) -> dict:
    x, y = _foreground_center()
    mx, my = pyautogui.position()
    if abs(mx - x) > 60 or abs(my - y) > 60:
        glide_to(x, y, 0.45)
    n = max(1, min(int(amount), 40))
    smooth_scroll(-n if direction.lower().startswith("d") else n)
    return {"ok": True, "scrolled": direction, "notches": n}


def _shape_points(shape: str, n: int = 240):
    shape = shape.lower()
    pts = []
    if shape == "star":
        verts = []
        for i in range(11):
            r = 1.0 if i % 2 == 0 else 0.42
            a = -math.pi / 2 + i * math.pi / 5
            verts.append((r * math.cos(a), r * math.sin(a)))
        for (x1, y1), (x2, y2) in zip(verts, verts[1:]):
            for k in range(24):
                t = k / 24
                pts.append((x1 + (x2 - x1) * t, y1 + (y2 - y1) * t))
        pts.append(verts[-1])
    elif shape == "heart":
        for i in range(n + 1):
            t = 2 * math.pi * i / n
            x = 16 * math.sin(t) ** 3
            y = 13 * math.cos(t) - 5 * math.cos(2 * t) - 2 * math.cos(3 * t) - math.cos(4 * t)
            pts.append((x / 17, -y / 17))
    elif shape == "spiral":
        for i in range(n * 2):
            t = i / (n * 2)
            a = t * 6 * math.pi
            pts.append((t * math.cos(a), t * math.sin(a)))
    elif shape == "infinity":
        for i in range(n + 1):
            t = 2 * math.pi * i / n
            d = 1 + math.sin(t) ** 2
            pts.append((math.cos(t) / d, math.sin(t) * math.cos(t) / d))
    elif shape == "arc_reactor":
        for ring in (1.0, 0.62):
            for i in range(n // 2 + 1):
                a = 2 * math.pi * i / (n // 2)
                pts.append((ring * math.cos(a), ring * math.sin(a)))
            pts.append(None)
        tri = [(0, -0.55), (0.48, 0.28), (-0.48, 0.28), (0, -0.55)]
        for (x1, y1), (x2, y2) in zip(tri, tri[1:]):
            for k in range(30):
                t = k / 30
                pts.append((x1 + (x2 - x1) * t, y1 + (y2 - y1) * t))
        pts.append(tri[-1])
    else:  # circle
        for i in range(n + 1):
            a = 2 * math.pi * i / n
            pts.append((math.cos(a), math.sin(a)))
    return pts


def draw_with_mouse(ctx, shape: str = "star", hold_button: bool = True, size_percent: float = 30,
                    center_x_percent: float = 50, center_y_percent: float = 55) -> dict:
    size = SCREEN_H * max(5, min(size_percent, 45)) / 100
    cx = SCREEN_W * center_x_percent / 100
    cy = SCREEN_H * center_y_percent / 100
    pts = _shape_points(shape)
    first = pts[0]
    glide_to(cx + first[0] * size, cy + first[1] * size, 0.6)
    ctx.overlay.trail(True)
    down = False
    try:
        for p in pts:
            if p is None:
                if down:
                    pyautogui.mouseUp()
                    down = False
                continue
            x, y = int(cx + p[0] * size), int(cy + p[1] * size)
            if not down and hold_button:
                pyautogui.moveTo(x, y, _pause=False)
                pyautogui.mouseDown()
                down = True
            pyautogui.moveTo(x, y, _pause=False)
            time.sleep(0.006)
    finally:
        if down:
            pyautogui.mouseUp()
        ctx.overlay.trail(False)
    return {"ok": True, "drew": shape}


def media_control(ctx, action: str, steps: int = 5) -> dict:
    keymap = {"play_pause": "playpause", "next": "nexttrack", "previous": "prevtrack",
              "volume_up": "volumeup", "volume_down": "volumedown", "mute": "volumemute"}
    key = keymap.get(action)
    if not key:
        return {"ok": False, "error": "unknown action"}
    n = max(1, min(int(steps), 50)) if action.startswith("volume") else 1
    for _ in range(n):
        if IS_X11:   # pyautogui's X11 backend has no media keys
            subprocess.run(["xdotool", "key", X11_MEDIA_KEYS[key]], timeout=3)
        else:
            pyautogui.press(key)
        time.sleep(0.03)
    return {"ok": True, "action": action}


X11_MEDIA_KEYS = {"playpause": "XF86AudioPlay", "nexttrack": "XF86AudioNext", "prevtrack": "XF86AudioPrev",
                  "volumeup": "XF86AudioRaiseVolume", "volumedown": "XF86AudioLowerVolume",
                  "volumemute": "XF86AudioMute"}
WINDOWS_COMBOS = {
    "show_desktop": ("win", "d"), "switch_window": ("alt", "tab"), "close_window": ("alt", "f4"),
    "maximize": ("win", "up"), "minimize": ("win", "down"), "snap_left": ("win", "left"),
    "snap_right": ("win", "right"), "task_view": ("win", "tab"), "new_tab": ("ctrl", "t"),
    "close_tab": ("ctrl", "w"), "screenshot_tool": ("win", "shift", "s"),
}
# Linux: action -> GNOME settings that hold the user's shortcut, and a default if there is none
GNOME_SHORTCUTS = {
    "show_desktop": ([("org.gnome.desktop.wm.keybindings", "show-desktop")], ("win", "d")),
    "switch_window": ([("org.gnome.desktop.wm.keybindings", "switch-windows")], ("alt", "tab")),
    "close_window": ([("org.gnome.desktop.wm.keybindings", "close")], ("alt", "f4")),
    "maximize": ([("org.gnome.desktop.wm.keybindings", "maximize"),
                  ("org.gnome.desktop.wm.keybindings", "toggle-maximized")], ("win", "up")),
    "minimize": ([("org.gnome.desktop.wm.keybindings", "minimize")], ("win", "h")),
    "snap_left": ([("org.gnome.mutter.keybindings", "toggle-tiled-left")], ("win", "left")),
    "snap_right": ([("org.gnome.mutter.keybindings", "toggle-tiled-right")], ("win", "right")),
    "task_view": ([("org.gnome.shell.keybindings", "toggle-overview")], ("win",)),
    "new_tab": ([], ("ctrl", "t")), "close_tab": ([], ("ctrl", "w")),
    "screenshot_tool": ([("org.gnome.shell.keybindings", "show-screenshot-ui")], ("print",)),
}


def _gnome_shortcut(schema: str, key: str) -> tuple | None:
    """The user's current shortcut, e.g. ['<Super>d'] -> ('win', 'd'). None if unset or not GNOME."""
    try:
        from gi.repository import Gio
        src = Gio.SettingsSchemaSource.get_default()
        sch = src.lookup(schema, True) if src else None
        if not sch or not sch.has_key(key):
            return None
        accels = Gio.Settings.new(schema).get_strv(key)
    except Exception:
        return None
    if not accels:
        return None
    mods = re.findall(r"<(\w+)>", accels[0])
    base = re.sub(r"<\w+>", "", accels[0]).lower()
    names = {"primary": "ctrl", "control": "ctrl", "ctrl": "ctrl", "super": "win", "alt": "alt", "shift": "shift"}
    keys = {"page_up": "pageup", "page_down": "pagedown", "return": "enter", "escape": "esc"}
    return (*(names.get(m.lower(), m.lower()) for m in mods), keys.get(base, base))


def window_control(ctx, action: str) -> dict:
    if action not in WINDOWS_COMBOS:
        return {"ok": False, "error": "unknown action"}
    if IS_WINDOWS:
        c = WINDOWS_COMBOS[action]
    else:
        settings, default = GNOME_SHORTCUTS[action]
        c = next((k for k in (_gnome_shortcut(*s) for s in settings) if k), default)
    pyautogui.hotkey(*c) if len(c) > 1 else pyautogui.press(c[0])
    time.sleep(0.5)
    return {"ok": True, "action": action}


def get_system_status(ctx) -> dict:
    vm = psutil.virtual_memory()
    du = psutil.disk_usage(SYSTEM_DISK)
    bat = psutil.sensors_battery()
    procs = []
    for p in psutil.process_iter(["name", "memory_info"]):
        try:
            procs.append((p.info["memory_info"].rss, p.info["name"]))
        except Exception:
            pass
    top = [n for _, n in sorted(procs, reverse=True)[:5]]
    return {
        "cpu_percent": psutil.cpu_percent(interval=0.4),
        "cpu_cores": psutil.cpu_count(),
        "ram_used_gb": round(vm.used / 1e9, 1), "ram_total_gb": round(vm.total / 1e9, 1),
        "disk_free_gb": round(du.free / 1e9, 1),
        "battery_percent": bat.percent if bat else None,
        "plugged_in": bat.power_plugged if bat else None,
        "uptime_hours": round((time.time() - psutil.boot_time()) / 3600, 1),
        "top_memory_processes": top,
        "machine": platform.node(), "os": os_name(),
    }


def get_weather(ctx, city: str = "") -> dict:
    try:
        data = json.loads(_fetch(f"https://wttr.in/{urllib.parse.quote(city)}?format=j1"))
        cur = data["current_condition"][0]
        area = data.get("nearest_area", [{}])[0]
        place = area.get("areaName", [{"value": city}])[0]["value"]
        today = data["weather"][0]
        return {
            "location": place, "condition": cur["weatherDesc"][0]["value"],
            "temp_c": cur["temp_C"], "feels_like_c": cur["FeelsLikeC"], "humidity": cur["humidity"],
            "wind_kmph": cur["windspeedKmph"], "max_c": today["maxtempC"], "min_c": today["mintempC"],
        }
    except Exception as exc:
        return {"ok": False, "error": f"weather service unavailable: {exc}"}


# ============================================================== builder tools
WORKSPACE = Path.home() / "Desktop" / "Jarvis Projects"


def _resolve(path: str) -> Path:
    p = Path(os.path.expandvars(os.path.expanduser(path)))
    return p if p.is_absolute() else WORKSPACE / p


def _open_with(path: Path, how: str = "default") -> None:
    if how == "vscode" and shutil.which("code"):
        _detached(["cmd", "/c", "code", str(path)] if IS_WINDOWS else ["code", str(path)])
    elif how == "browser":
        webbrowser.open(path.as_uri())
    elif how == "explorer":   # show it in the file manager
        if IS_WINDOWS:
            subprocess.Popen(["explorer", "/select,", str(path)])
        else:
            _detached(["xdg-open", str(path if path.is_dir() else path.parent)])
    elif IS_WINDOWS:
        os.startfile(str(path))
    else:
        _detached(["xdg-open", str(path)])


PAGE = None          # server.PageBridge, set by server.py at startup
LAST_BUILT: Path | None = None
BRIDGE_TAG = f'<script src="http://localhost:{os.getenv("JARVIS_PORT", "8765")}/static/bridge.js"></script>'


def _inject_bridge(html: str) -> str:
    """Pages Jarvis builds get a tiny script so Jarvis can scroll/highlight them while presenting."""
    if "static/bridge.js" in html:
        return html
    i = html.lower().rfind("</body>")
    return html[:i] + BRIDGE_TAG + "\n" + html[i:] if i >= 0 else html + "\n" + BRIDGE_TAG


def build_web_page(ctx, description: str, filename: str = "") -> dict:
    """Generate a complete single-file website/app/game with Gemini, streaming the code live to the HUD."""
    base = re.sub(r"\.html?$", "", (filename or description).strip(), flags=re.I)
    name = re.sub(r"[^\w-]+", "-", base.lower()).strip("-")[:40] or "page"
    if not name.endswith(".html"):
        name += ".html"
    path = WORKSPACE / name
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    ctx.emit({"type": "code_start", "file": name})
    code = ctx.brain.write_code(description, on_chunk=lambda t: ctx.emit({"type": "code", "chunk": t}))
    path.write_text(_inject_bridge(code), encoding="utf-8")
    global LAST_BUILT
    LAST_BUILT = path
    if PAGE:
        PAGE.connected.clear()
    ctx.emit({"type": "code_end", "file": name, "chars": len(code)})
    if shutil.which("code"):
        _open_with(path, "vscode")
        time.sleep(1.2)
    _open_with(path, "browser")
    time.sleep(1.0)
    return {"ok": True, "file": str(path), "size_chars": len(code), "opened": "browser" + (" + VS Code" if shutil.which("code") else "")}


def write_file(ctx, path: str, content: str, open_with: str = "none") -> dict:
    p = _resolve(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    ctx.emit({"type": "code_start", "file": p.name})
    ctx.emit({"type": "code", "chunk": content})
    if p.suffix.lower() in (".html", ".htm"):
        content = _inject_bridge(content)
        global LAST_BUILT
        LAST_BUILT = p
        if PAGE:
            PAGE.connected.clear()
    p.write_text(content, encoding="utf-8")
    ctx.emit({"type": "code_end", "file": p.name, "chars": len(content)})
    if open_with != "none":
        _open_with(p, open_with)
    return {"ok": True, "file": str(p), "bytes": len(content.encode())}


def read_file(ctx, path: str) -> dict:
    p = _resolve(path)
    if not p.exists():
        return {"ok": False, "error": f"{p} does not exist"}
    return {"ok": True, "file": str(p), "content": p.read_text(encoding="utf-8", errors="replace")[:12000]}


def open_path(ctx, path: str, open_with: str = "default") -> dict:
    p = _resolve(path)
    if not p.exists():
        return {"ok": False, "error": f"{p} does not exist"}
    _open_with(p, open_with)
    time.sleep(0.8)
    return {"ok": True, "opened": str(p)}


if IS_WINDOWS:
    SHELL, PROMPT = "PowerShell", "PS> "
    DANGEROUS = re.compile(
        r"(format-volume|format\s+[a-z]:|diskpart|bcdedit|stop-computer|restart-computer|shutdown|"
        r"remove-item\s+.*(c:\\(windows|users)?\\?\s*$|-recurse.*c:\\\s)|rd\s+/s\s+/q\s+c:\\|reg\s+delete|"
        r"set-executionpolicy|invoke-expression.*http|iex.*http|cipher\s+/w)",
        re.I,
    )
else:
    SHELL, PROMPT = "bash", "$ "
    DANGEROUS = re.compile(
        r"(\bsudo\b|\bsu\s|\bpkexec\b|\bmkfs|\bfdisk\b|\bparted\b|\bwipefs\b|\bshred\b|\bdd\b.*\bof=/dev/|>\s*/dev/[sn]|"
        r"(^|[;&|(]\s*)(shutdown|reboot|poweroff|halt|init\s+[06])\b|systemctl\s+(poweroff|reboot|halt|suspend)|"
        r"\brm\s+(-\S*\s+)*(/|/\*|~|~/|~/\*|\$HOME/?|/(bin|boot|etc|home|lib\w*|opt|root|sbin|srv|usr|var)/?)(\s|$)|"
        r"\bchmod\s+-R\s+\S+\s+/(\s|$)|\bchown\s+-R\s+\S+\s+/(\s|$)|:\(\)\s*\{|"
        r"(curl|wget)\b.*\|\s*(sudo\s+)?(ba|z|da)?sh\b|find\s+/\s.*-delete)",
        re.I,
    )


def run_command(ctx, command: str, timeout_seconds: int = 60) -> dict:
    if DANGEROUS.search(command):
        return {"ok": False, "error": "Blocked: this command looks destructive or needs admin rights. "
                                      "Ask the user to run it manually."}
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    ctx.emit({"type": "code_start", "file": SHELL})
    ctx.emit({"type": "code", "chunk": f"{PROMPT}{command}\n"})
    if IS_WINDOWS:
        argv, extra = ["powershell", "-NoProfile", "-Command", command], {"creationflags": subprocess.CREATE_NO_WINDOW}
    else:   # no stdin: a command waiting for input fails instead of hanging until the timeout
        argv, extra = ["bash", "-c", command], {"stdin": subprocess.DEVNULL}
    try:
        r = subprocess.run(argv, cwd=WORKSPACE, capture_output=True, text=True,
                           timeout=max(5, min(timeout_seconds, 300)), encoding="utf-8", errors="replace", **extra)
        out = (r.stdout or "") + (("\n[stderr]\n" + r.stderr) if r.stderr.strip() else "")
        ctx.emit({"type": "code", "chunk": out[-4000:]})
        ctx.emit({"type": "code_end", "file": SHELL, "chars": len(out)})
        return {"ok": r.returncode == 0, "exit_code": r.returncode, "output": out[-6000:]}
    except subprocess.TimeoutExpired:
        ctx.emit({"type": "code_end", "file": SHELL, "chars": 0})
        return {"ok": False, "error": "timed out"}


def _page_matches(info: dict, target: Path | None) -> bool:
    if not info or not target:
        return bool(info)
    url = urllib.parse.unquote(info.get("url", ""))
    return url.replace("\\", "/").lower().endswith(target.name.lower())


def present_page(ctx, path: str = "", focus: str = "") -> dict:
    """Walk through a page like a human presenter: scroll section by section, point at it, narrate it."""
    target = _resolve(path) if path else LAST_BUILT
    if target and not target.exists():
        target = None
    info = PAGE.info if (PAGE and PAGE.ws) else {}
    if target and not _page_matches(info, target):
        PAGE.connected.clear()
        _open_with(target, "browser")
        PAGE.connected.wait(10)
        time.sleep(0.8)
        info = PAGE.info if PAGE.ws else {}
    if info.get("sections"):
        return _present_with_bridge(ctx, info, focus)
    return _present_visually(ctx, focus)


def _present_with_bridge(ctx, info: dict, focus: str) -> dict:
    secs = info["sections"][:14]
    lines = ctx.brain.narrate(info.get("title", ""), secs, focus)
    done = 0
    for i, (sec, line) in enumerate(zip(secs, lines)):
        if ctx.interrupted.is_set():
            break
        r = PAGE.call("goto", index=sec["index"], total=len(secs), timeout=8)
        ctx.emit({"type": "present", "index": i + 1, "total": len(secs), "heading": sec.get("heading", "")})
        print(f"[present] {i + 1}/{len(secs)} {sec.get('heading', '')[:50]}")
        pt = r.get("point") if r.get("ok") else None
        if pt and 0 <= pt[0] < SCREEN_W and 0 <= pt[1] < SCREEN_H:
            glide_to(pt[0] - r.get("width", 300) // 3, pt[1], 0.55)   # point at the heading...
            pyautogui.moveRel(min(r.get("width", 300) // 2, 500), 0, duration=0.5,
                              tween=pyautogui.easeInOutQuad)               # ...and underline it
        if not ctx.speak_and_wait(line):
            break
        done += 1
        cp = r.get("click") if r.get("ok") else None
        if cp and 0 <= cp[0] < SCREEN_W and 0 <= cp[1] < SCREEN_H and not ctx.interrupted.is_set():
            # demo the interactivity like a human presenter: move to the button and click it
            ctx.overlay.target(cp[0], cp[1], r.get("click_label") or "interactive element")
            glide_to(cp[0], cp[1], 0.6)
            time.sleep(0.35)
            pyautogui.click()
            time.sleep(1.4)
    try:
        PAGE.call("clear", timeout=3)
    except Exception:
        pass
    return {"ok": True, "sections_presented": done, "of": len(secs),
            "note": "The full narration was ALREADY spoken aloud while scrolling. Reply with ONE short closing sentence only, do not repeat the content."}


def _present_visually(ctx, focus: str) -> dict:
    """Any page/app (no bridge): look -> narrate -> scroll -> repeat until the bottom."""
    from PIL import ImageChops, ImageStat
    x, y = _foreground_center()
    glide_to(x, y, 0.5)
    said, prev, done = [], None, 0
    for _ in range(8):
        if ctx.interrupted.is_set():
            break
        ctx.overlay.scan()
        with ctx.overlay.hidden():
            jpeg, img = grab_screen()
        small = img.convert("L").resize((96, 54))
        if prev is not None and ImageStat.Stat(ImageChops.difference(small, prev)).mean[0] < 2.0:
            break   # nothing changed after scrolling -> reached the bottom
        prev = small
        ctx.emit({"type": "vision", "image": jpeg, "point": None, "label": "PRESENTING"})
        q = ("You are J.A.R.V.I.S. presenting what is on this screen to a live audience, like a confident human "
             "presenter. In 2 natural spoken sentences, explain the part currently visible. No markdown. "
             + (f"Focus on: {focus}. " if focus else "")
             + (f"Do not repeat what you already said: {' | '.join(said[-3:])}" if said else "Start with a short hook."))
        line = ctx.brain.describe(jpeg, q)
        said.append(line)
        if not ctx.speak_and_wait(line):
            break
        done += 1
        smooth_scroll(-8)
        time.sleep(0.6)
    return {"ok": True, "screens_presented": done,
            "note": "The narration was ALREADY spoken aloud. Reply with ONE short closing sentence only."}


NAMED_COLORS = {
    "blue": "#00e5ff", "cyan": "#00e5ff", "default": "#00e5ff", "teal": "#14f0c8", "red": "#ff2a3d",
    "crimson": "#ff1744", "orange": "#ff8a1c", "gold": "#ffc83d", "yellow": "#ffe23d", "green": "#2bff88",
    "lime": "#a6ff2b", "emerald": "#19e6a0", "purple": "#b54dff", "violet": "#8f5bff", "pink": "#ff3fb4",
    "magenta": "#ff2bd6", "white": "#e8f6ff", "silver": "#c9d6e3", "ice": "#9be8ff", "navy": "#3d7bff",
}
PRESETS = {
    "iron man": ("#ff2a3d", "#ffc83d"), "mark 3": ("#ff2a3d", "#ffc83d"), "hulk": ("#2bff88", "#b54dff"),
    "ultron": ("#ff1744", "#c9d6e3"), "vision": ("#ffe23d", "#2bff88"), "stealth": ("#e8f6ff", "#8a9bb0"),
    "thanos": ("#b54dff", "#ffc83d"), "matrix": ("#2bff88", "#a6ff2b"), "default": ("#00e5ff", "#ff9d1c"),
    "jarvis": ("#00e5ff", "#ff9d1c"), "friday": ("#ff8a1c", "#00e5ff"), "spider": ("#ff2a3d", "#3d7bff"),
}


def _color(c: str) -> str | None:
    c = (c or "").strip().lower()
    if re.fullmatch(r"#?[0-9a-f]{6}", c):
        return "#" + c.lstrip("#")
    return NAMED_COLORS.get(c)


def _color_name(hex_: str) -> str:
    """Nearest friendly name for a hex color (for the on-screen label)."""
    rgb = [int(hex_[i:i + 2], 16) for i in (1, 3, 5)]
    best = min((n for n in NAMED_COLORS if n != "default"),
               key=lambda n: sum((a - int(NAMED_COLORS[n][i:i + 2], 16)) ** 2 for a, i in zip(rgb, (1, 3, 5))))
    return best


def customize_interface(ctx, color: str = "", secondary_color: str = "", preset: str = "", layout: str = "") -> dict:
    primary = secondary = None
    if preset:
        key = next((k for k in PRESETS if k in preset.lower()), None)
        if key:
            primary, secondary = PRESETS[key]
    if not primary:                      # a recognised preset wins over loose colors the model may add
        primary = _color(color)
        secondary = _color(secondary_color)
    if primary and not secondary:
        secondary = "#ffc83d" if primary in ("#ff2a3d", "#ff1744") else "#ff9d1c"
    if not primary and not layout:
        return {"ok": False, "error": f"unknown color/preset. Colors: {', '.join(NAMED_COLORS)}. Presets: {', '.join(PRESETS)}"}
    if primary:
        ctx.broadcast({"type": "theme", "primary": primary, "secondary": secondary,
                       "label": preset or _color_name(primary)})
        ctx.overlay.set_theme(primary, secondary)
    if layout:
        ctx.broadcast({"type": "layout", "mode": layout})
    time.sleep(1.2)
    return {"ok": True, "primary": primary, "secondary": secondary, "layout": layout or "unchanged"}


def wait(ctx, seconds: float = 1.5) -> dict:
    time.sleep(max(0.1, min(float(seconds), 10)))
    return {"ok": True}


# ============================================================== registry
TOOLS = {
    "open_application": (open_application, "Open/launch any installed application by name (e.g. 'notepad', 'spotify', 'chrome', 'vs code', 'paint').",
                         {"name": {"type": "string"}}, ["name"]),
    "open_website": (open_website, "Open a website URL in the default browser.",
                     {"url": {"type": "string"}}, ["url"]),
    "google_search": (google_search, "Search Google in the browser and show results.",
                      {"query": {"type": "string"}}, ["query"]),
    "play_youtube": (play_youtube, "Find and immediately play a video or song on YouTube.",
                     {"query": {"type": "string"}}, ["query"]),
    "type_text": (type_text, "Type text with the keyboard into the currently focused window.",
                  {"text": {"type": "string"}, "press_enter": {"type": "boolean"}}, ["text"]),
    "press_keys": (press_keys, "Press a key or keyboard shortcut, e.g. 'enter', 'ctrl+s', 'ctrl+shift+n', 'f5', "
                   + ("'win+e'." if IS_WINDOWS else "'win' (the Super key)."),
                   {"keys": {"type": "string"}}, ["keys"]),
    "click_on_screen": (click_on_screen, "Look at the screen, find a UI element from a natural-language description (button, link, icon, text, field) and move the mouse there and click it.",
                        {"target": {"type": "string", "description": "Visual description of what to click, e.g. 'the Search box', 'the first video thumbnail', 'the red X close button'"},
                         "double_click": {"type": "boolean"}, "right_click": {"type": "boolean"}}, ["target"]),
    "look_at_screen": (look_at_screen, "Take a screenshot and visually analyse it to answer a question about what is currently on screen.",
                       {"question": {"type": "string"}}, []),
    "look_through_camera": (look_through_camera, "Look at the user through the WEBCAM: what they are holding, wearing, doing, showing, how many fingers, text they hold up, their surroundings. Returns a factual observation; you then answer in character.",
                            {"question": {"type": "string", "description": "what to look for, e.g. 'what am I holding?'"}}, []),
    "scroll": (scroll, "Scroll the active window (page, document, app) up or down smoothly, like a human. amount = wheel notches (5 = about half a page, 10 = a page).",
               {"direction": {"type": "string", "enum": ["up", "down"]}, "amount": {"type": "integer"}}, ["direction"]),
    "draw_with_mouse": (draw_with_mouse, "Move/drag the mouse in a shape. Use hold_button=true to actually draw in a paint app (maximize Paint first). With hold_button=false it just shows off cursor control.",
                        {"shape": {"type": "string", "enum": ["star", "heart", "circle", "spiral", "infinity", "arc_reactor"]},
                         "hold_button": {"type": "boolean"}, "size_percent": {"type": "number"},
                         "center_x_percent": {"type": "number"}, "center_y_percent": {"type": "number"}}, ["shape"]),
    "media_control": (media_control, "Control media playback and system volume.",
                      {"action": {"type": "string", "enum": ["play_pause", "next", "previous", "volume_up", "volume_down", "mute"]},
                       "steps": {"type": "integer", "description": "volume steps (each = 2%)"}}, ["action"]),
    "window_control": (window_control, "Manage windows and tabs.",
                       {"action": {"type": "string", "enum": ["show_desktop", "switch_window", "close_window", "maximize", "minimize",
                                                              "snap_left", "snap_right", "task_view", "new_tab", "close_tab", "screenshot_tool"]}}, ["action"]),
    "get_system_status": (get_system_status, "Get live diagnostics of this computer: CPU, RAM, disk, battery, uptime, heaviest processes.", {}, []),
    "get_weather": (get_weather, "Get current weather for a city (empty = auto-detect location).",
                    {"city": {"type": "string"}}, []),
    "build_web_page": (build_web_page, "FASTEST way to create any web page, website, landing page, dashboard, web app or browser game. Writes a complete polished single-file HTML/CSS/JS app (code streams live on the HUD), saves it, and opens it in the browser and VS Code. Use this for ANY 'build/make/create a page/site/app/game' request.",
                       {"description": {"type": "string", "description": "Detailed description of what to build: purpose, content, style, features."},
                        "filename": {"type": "string", "description": "short file name, e.g. 'portfolio'"}}, ["description"]),
    "write_file": (write_file, "Create or overwrite a file with given content (code, text, csv, python script...). Relative paths go to Desktop/Jarvis Projects.",
                   {"path": {"type": "string"}, "content": {"type": "string"},
                    "open_with": {"type": "string", "enum": ["none", "default", "vscode", "browser", "explorer"]}}, ["path", "content"]),
    "read_file": (read_file, "Read a text file's content.", {"path": {"type": "string"}}, ["path"]),
    "open_path": (open_path, "Open an existing file or folder with an app.",
                  {"path": {"type": "string"}, "open_with": {"type": "string", "enum": ["default", "vscode", "browser", "explorer"]}}, ["path"]),
    "run_command": (run_command, f"Run a {SHELL} command and get its output (working dir: Desktop/Jarvis Projects). Use for creating folders, running python/node scripts, git, pip/npm, listing files, system queries. Never destructive.",
                       {"command": {"type": "string"}, "timeout_seconds": {"type": "integer"}}, ["command"]),
    "present_page": (present_page, "PRESENT / walk through / explain a web page or presentation like a real human presenter: scrolls section by section, points at each part with the mouse, and narrates it aloud in sync. Use for 'present it', 'scroll through it and explain', 'walk me through'. With no path it presents the last page you built; otherwise give the file path. For any other page already on screen, call it with no path.",
                     {"path": {"type": "string", "description": "optional file path; empty = last built page or current screen"},
                      "focus": {"type": "string", "description": "optional angle, e.g. 'explain the design choices'"}}, []),
    "customize_interface": (customize_interface, "Change YOUR OWN HUD interface look live: color theme (e.g. 'red', 'green', '#ff00aa') or a preset ('iron man', 'hulk', 'ultron', 'vision', 'stealth', 'thanos', 'matrix', 'friday', 'default'), and layout ('minimal' hides side panels, 'full' shows all).",
                            {"color": {"type": "string"}, "secondary_color": {"type": "string"},
                             "preset": {"type": "string"}, "layout": {"type": "string", "enum": ["minimal", "full"]}}, []),
    "wait": (wait, "Pause for a moment, e.g. to let an app or web page finish loading.",
             {"seconds": {"type": "number"}}, []),
}


def tool_schemas() -> list[dict]:
    """Provider-neutral JSON-schema tool definitions (used by Gemini and OpenAI-compatible APIs)."""
    out = []
    for name, (_, desc, props, req) in TOOLS.items():
        schema = {"type": "object", "properties": props}
        if req:
            schema["required"] = req
        out.append({"name": name, "description": desc, "parameters": schema})
    return out


def execute(ctx, name: str, args: dict):
    if name not in TOOLS:
        return {"ok": False, "error": f"unknown tool {name}"}
    fn = TOOLS[name][0]
    args = {k: v for k, v in (args or {}).items() if v is not None}   # some models send explicit nulls
    try:
        return fn(ctx, **args)
    except pyautogui.FailSafeException:
        return {"ok": False, "error": "ABORTED by user (mouse fail-safe triggered)."}
    except TypeError as exc:
        return {"ok": False, "error": f"bad arguments: {exc}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def os_name() -> str:
    """e.g. 'Windows 11' or 'Ubuntu 26.04.1 LTS (GNOME, Wayland)'."""
    if not sys.platform.startswith("linux"):
        return f"{platform.system()} {platform.release()}"
    try:
        name = platform.freedesktop_os_release().get("PRETTY_NAME", "Linux")
    except OSError:
        name = "Linux"
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").split(":")[-1]
    return f"{name} ({', '.join(filter(None, [desktop, 'Wayland' if IS_WAYLAND else 'X11']))})"


def now_string() -> str:
    return datetime.datetime.now().strftime("%A %d %B %Y, %H:%M")
