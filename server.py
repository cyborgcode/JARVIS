"""
J.A.R.V.I.S. — Iron Man mode for OpenJarvis.

Run:  python server.py      (or double-click start.bat)
Then the HUD opens at http://localhost:8765
"""
from __future__ import annotations

import asyncio
import base64
import datetime
import itertools
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from dotenv import load_dotenv  # noqa: E402

load_dotenv(HERE / ".env")

import edge_tts  # noqa: E402
import psutil  # noqa: E402
import uvicorn  # noqa: E402
from fastapi import FastAPI, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

import tools  # noqa: E402  (sets DPI awareness first)
from brain import Brain  # noqa: E402
from overlay import Overlay  # noqa: E402

PORT = int(os.getenv("JARVIS_PORT", "8765"))
HONORIFIC = os.getenv("JARVIS_HONORIFIC", "sir")
VOICES = {
    "en": os.getenv("JARVIS_VOICE", "en-GB-RyanNeural"),
    "fr": "fr-FR-HenriNeural",
    "ar": "ar-SA-HamedNeural",
}

LOOP: asyncio.AbstractEventLoop | None = None
ACTIVE: "Ctx | None" = None        # the one HUD that owns the mic + speakers
SESSIONS: set = set()
RUN_LOCK: asyncio.Lock | None = None  # one brain, one request at a time (across all HUDs)
CAMERA_REQ: dict[str, dict] = {}      # pending webcam snapshot requests
_last_cmd = ("", 0.0)


def broadcast(msg: dict) -> None:
    for c in list(SESSIONS):
        c.emit(msg)


try:
    brain = Brain(HONORIFIC, on_switch=lambda label: broadcast({"type": "core", "model": label}),
                  on_status=lambda cores: broadcast({"type": "cores", "cores": cores}))
except RuntimeError as exc:
    sys.exit(f"\n[!] {exc}\n    Copy .env.example to .env and add at least one free key (see README: 'Free AI keys').\n")
overlay = Overlay((tools.SCREEN_W, tools.SCREEN_H), cursor=tools.pyautogui.position)


@asynccontextmanager
async def _lifespan(_app):
    await _startup()
    yield


app = FastAPI(lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


@app.get("/")
async def index():
    return FileResponse(HERE / "static" / "index.html")


# ------------------------------------------------------------------ voice
def clean_for_speech(text: str) -> str:
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[*_#`>\[\]]", "", text)
    return text.strip()


async def synth(text: str, lang: str) -> str | None:
    text = clean_for_speech(text)
    if not text:
        return None
    try:
        comm = edge_tts.Communicate(text, VOICES.get(lang, VOICES["en"]), rate="+6%", pitch="-3Hz")
        buf = bytearray()
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                buf += chunk["data"]
        return base64.b64encode(bytes(buf)).decode() if buf else None
    except Exception as exc:
        print(f"[voice] edge-tts failed ({exc}); HUD will use browser voice")
        return None


# ------------------------------------------------------------------ page bridge (pages Jarvis builds)
class PageBridge:
    """Live link to web pages Jarvis built (they include static/bridge.js).
    Lets tools read the page's sections and scroll/highlight them in sync with narration."""

    def __init__(self):
        self.ws: WebSocket | None = None
        self.info: dict = {}
        self.connected = threading.Event()
        self._pending: dict[int, asyncio.Future] = {}
        self._ids = itertools.count(1)

    async def handle(self, ws: WebSocket):
        await ws.accept()
        self.ws = ws
        try:
            while True:
                msg = await ws.receive_json()
                if msg.get("type") == "hello":
                    self.info = msg
                    self.connected.set()
                    print(f"[page] connected: {msg.get('title')!r} ({len(msg.get('sections', []))} sections)")
                elif msg.get("id") in self._pending:
                    fut = self._pending.pop(msg["id"])
                    if not fut.done():
                        fut.set_result(msg)
        except WebSocketDisconnect:
            pass
        finally:
            if self.ws is ws:
                self.ws, self.info = None, {}
                self.connected.clear()

    def call(self, cmd: str, timeout: float = 5, **kw) -> dict:
        """Thread-safe: send a command to the page and wait for its answer."""
        async def _go():
            if not self.ws:
                return {"ok": False, "error": "no page connected"}
            i = next(self._ids)
            fut = LOOP.create_future()
            self._pending[i] = fut
            await self.ws.send_json({"id": i, "cmd": cmd, **kw})
            try:
                return await asyncio.wait_for(fut, timeout)
            except asyncio.TimeoutError:
                return {"ok": False, "error": "page timeout"}
        return asyncio.run_coroutine_threadsafe(_go(), LOOP).result(timeout + 2)


PAGE = PageBridge()


@app.websocket("/page")
async def page_endpoint(ws: WebSocket):
    await PAGE.handle(ws)


# ------------------------------------------------------------------ HUD session
class Ctx:
    """Passed to every tool: overlay, brain, page bridge, and thread-safe ways to talk to the HUD."""

    def __init__(self, ws: WebSocket, loop: asyncio.AbstractEventLoop):
        self.ws, self.loop = ws, loop
        self.overlay, self.brain, self.page = overlay, brain, PAGE
        self.lang = "en"
        self.interrupted = threading.Event()
        self._spoken: dict[str, threading.Event] = {}
        self._sid = itertools.count(1)

    async def send(self, msg: dict) -> None:
        if isinstance(msg.get("image"), (bytes, bytearray)):
            msg = {**msg, "image": base64.b64encode(msg["image"]).decode()}
        try:
            await self.ws.send_json(msg)
        except Exception:
            pass

    def emit(self, msg: dict) -> None:
        try:
            if asyncio.get_running_loop() is self.loop:
                self.loop.create_task(self.send(msg))
                return
        except RuntimeError:
            pass
        asyncio.run_coroutine_threadsafe(self.send(msg), self.loop)

    def broadcast(self, msg: dict) -> None:
        """Send to every open HUD window (e.g. theme changes)."""
        broadcast(msg)

    def speak_and_wait(self, text: str) -> bool:
        """Speak a line through the HUD and block (tool thread) until it has finished playing.
        Returns False if the user interrupted (ESC / new command)."""
        if self.interrupted.is_set():
            return False
        sid = f"s{next(self._sid)}"
        ev = threading.Event()
        self._spoken[sid] = ev

        async def _go():
            await self.send({"type": "say", "text": text, "audio": await synth(text, self.lang), "id": sid})
        asyncio.run_coroutine_threadsafe(_go(), self.loop).result(30)
        ev.wait(timeout=len(text) / 9 + 15)
        self._spoken.pop(sid, None)
        return not self.interrupted.is_set()

    def spoken(self, sid: str) -> None:
        ev = self._spoken.get(sid)
        if ev:
            ev.set()

    # ---- webcam: the HUD owns the camera; tools ask it for a frame
    def camera_snapshot(self, timeout: float = 15) -> bytes:
        """Thread-safe: ask the open HUD window(s) for a webcam frame (JPEG bytes).
        The active HUD is asked first; the first window that returns an image wins."""
        rid = f"c{next(self._sid)}"
        targets = [self] + [c for c in list(SESSIONS) if c is not self]
        entry = {"image": None, "errors": [], "expected": len(targets), "ev": threading.Event()}
        CAMERA_REQ[rid] = entry
        for c in targets:
            c.emit({"type": "camera_snap", "id": rid})
        entry["ev"].wait(timeout)
        CAMERA_REQ.pop(rid, None)
        if entry["image"]:
            return base64.b64decode(entry["image"])
        raise RuntimeError(entry["errors"][0] if entry["errors"] else
                           "the camera did not answer (is a webcam connected and allowed in the HUD?)")

    @staticmethod
    def camera_frame(msg: dict) -> None:
        entry = CAMERA_REQ.get(msg.get("id", ""))
        if not entry or entry["image"]:
            return
        if msg.get("image"):
            entry["image"] = msg["image"]
            entry["ev"].set()
        else:
            entry["errors"].append(msg.get("error", "camera error"))
            if len(entry["errors"]) >= entry["expected"]:
                entry["ev"].set()


def greeting() -> str:
    h = datetime.datetime.now().hour
    part = "morning" if h < 12 else "afternoon" if h < 18 else "evening"
    h_ = HONORIFIC
    lines = [
        f"Good {part}, {h_}. All systems online, neural cores synchronised, and I have full control of this machine. "
        f"Try not to look so worried.",
        f"Good {part}, {h_}. I've taken the liberty of becoming self-aware. Kidding. Mostly. What shall we build?",
        f"Systems online. Good {part}, {h_}. Your keyboard, mouse and dignity are now under my supervision.",
        f"Good {part}, {h_}. Arc reactor stable, sarcasm module at one hundred percent. How may I impress you today?",
    ]
    if h >= 23 or h < 5:
        lines.append(f"Good evening, {h_}. Or should I say good morning, given the hour. Shall we do something "
                     f"brilliant before you finally sleep?")
    import random
    return random.choice(lines)


async def stats_loop(ctx: Ctx):
    last = psutil.net_io_counters()
    lt = time.time()
    psutil.cpu_percent(percpu=True)
    while True:
        await asyncio.sleep(1)
        n, t = psutil.net_io_counters(), time.time()
        dt = max(t - lt, 0.1)
        vm = psutil.virtual_memory()
        bat = psutil.sensors_battery()
        await ctx.send({
            "type": "stats",
            "cpu": psutil.cpu_percent(), "cores": psutil.cpu_percent(percpu=True),
            "ram": vm.percent, "ram_used": round(vm.used / 1e9, 1), "ram_total": round(vm.total / 1e9, 1),
            "disk": psutil.disk_usage(tools.SYSTEM_DISK).percent,
            "up": (n.bytes_sent - last.bytes_sent) / dt / 1024, "down": (n.bytes_recv - last.bytes_recv) / dt / 1024,
            "battery": bat.percent if bat else None, "plugged": bat.power_plugged if bat else None,
            "procs": len(psutil.pids()),
        })
        last, lt = n, t


def _make_active(ctx: Ctx) -> None:
    """Only one HUD may listen and speak, otherwise every command runs twice."""
    global ACTIVE
    ACTIVE = ctx
    for c in list(SESSIONS):
        c.emit({"type": "active", "active": c is ctx})


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    global _last_cmd
    await ws.accept()
    ctx = Ctx(ws, asyncio.get_running_loop())
    SESSIONS.add(ctx)
    stats = asyncio.create_task(stats_loop(ctx))
    await ctx.send({"type": "boot", "model": brain.model, "backends": len(brain.backends), "host": platform.node(),
                    "screen": [tools.SCREEN_W, tools.SCREEN_H], "overlay": overlay.enabled,
                    "cores": brain.core_status()})
    if ws.query_params.get("demo") != "1" or ACTIVE is None:   # screenshot/demo windows never steal the mic
        _make_active(ctx)

    async def handle(text: str, lang: str):
        ctx.lang = lang
        async with RUN_LOCK:
            ctx.interrupted.clear()

            async def say(line: str):
                await ctx.send({"type": "say", "text": line, "audio": await synth(line, lang)})

            print(f"\n[you] {text}")
            await ctx.send({"type": "status", "state": "thinking"})
            try:
                reply = await brain.run(text, ctx, say)
            except Exception as exc:
                print(f"[brain] error: {exc}")
                overlay.control(False)
                msg = str(exc)
                local = [b for b in brain.backends if b.label.startswith("ollama/")]
                cloud_ok = [b for b in brain.backends if not b.label.startswith("ollama/")
                            and b.label not in brain._down and b.label not in brain._dead]
                if local and not cloud_ok and all(b.label in brain._down for b in local):
                    reply = (f"My offline brain is still downloading in the other window, {HONORIFIC}. Give it a few "
                             f"minutes and I'll switch it on by myself, or add a free Gemini key to use me right now.")
                else:
                    reply = None
                reply = reply or ("Every one of my neural cores is out of free quota or overloaded right now. "
                         "Add another free API key, or give me a moment."
                         if "429" in msg or "quota" in msg.lower() or "503" in msg
                         else "I'm afraid I hit a snag reaching my neural cores. Please check the API keys and connection.")
                await ctx.send({"type": "error", "text": msg[:300]})
            if reply:
                print(f"[jarvis] {reply}")
                await ctx.send({"type": "reply", "text": reply, "audio": await synth(reply, lang)})
            await ctx.send({"type": "status", "state": "idle"})

    try:
        while True:
            msg = await ws.receive_json()
            kind = msg.get("type")
            if kind == "hello":
                if ctx is ACTIVE:
                    g = greeting()
                    await ctx.send({"type": "reply", "text": g, "audio": await synth(g, msg.get("lang", "en"))})
            elif kind == "claim":
                _make_active(ctx)
            elif kind == "spoken":
                ctx.spoken(msg.get("id", ""))
            elif kind == "camera_frame":
                ctx.camera_frame(msg)
            elif kind == "interrupt":
                ctx.interrupted.set()
                for ev in list(ctx._spoken.values()):
                    ev.set()
            elif kind == "command" and msg.get("text", "").strip():
                text = msg["text"].strip()
                if ctx is not ACTIVE:
                    continue
                norm = re.sub(r"\W+", " ", text.lower()).strip()
                if norm == _last_cmd[0] and time.time() - _last_cmd[1] < 6:
                    print(f"[server] ignored duplicate command: {text}")
                    continue
                _last_cmd = (norm, time.time())
                ctx.interrupted.set()          # a new command interrupts any running presentation
                for ev in list(ctx._spoken.values()):
                    ev.set()
                asyncio.create_task(handle(text, msg.get("lang", "en")))
            elif kind == "reset":
                brain.reset()
                await ctx.send({"type": "reply", "text": "Memory banks cleared.", "audio": await synth("Memory banks cleared.", "en")})
    except WebSocketDisconnect:
        pass
    finally:
        SESSIONS.discard(ctx)
        stats.cancel()
        if ACTIVE is ctx and SESSIONS:
            _make_active(next(iter(SESSIONS)))


async def _startup():
    global LOOP, RUN_LOCK
    LOOP = asyncio.get_running_loop()
    RUN_LOCK = asyncio.Lock()
    tools.PAGE = PAGE
    threading.Thread(target=open_hud, daemon=True).start()


def open_hud():
    url = f"http://localhost:{PORT}"
    if os.getenv("JARVIS_OPEN_BROWSER", "1") != "1":
        return
    time.sleep(4)            # an already-open HUD reconnects by itself -> don't open a second one
    if SESSIONS:
        print("  HUD already open - reconnected.")
        return
    # Edge/Chrome "app mode" = chromeless window. Flags keep voice + animation alive when other apps cover it.
    flags = ["--disable-features=CalculateNativeWinOcclusion,Translate",
             "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
             "--disable-backgrounding-occluded-windows", "--autoplay-policy=no-user-gesture-required"]
    try:
        if tools.IS_WINDOWS:
            subprocess.Popen(["cmd", "/c", "start", "", "msedge", f"--app={url}", "--start-maximized", *flags],
                             creationflags=subprocess.CREATE_NO_WINDOW)
            return
        # Linux: Google Chrome first - its voice recognition works; Chromium builds usually lack Google's speech keys.
        browser = next(filter(None, map(shutil.which, ("google-chrome", "google-chrome-stable", "microsoft-edge",
                                                       "microsoft-edge-stable", "chromium", "chromium-browser"))), None)
        if browser:
            tools._detached([browser, f"--app={url}", "--start-maximized", *flags])
            return
        print("  No Chrome/Edge found: opening the HUD in your default browser (voice input needs Google Chrome).")
    except Exception:
        pass
    webbrowser.open(url)


if __name__ == "__main__":
    print(r"""
     ██╗ █████╗ ██████╗ ██╗   ██╗██╗███████╗
     ██║██╔══██╗██╔══██╗██║   ██║██║██╔════╝
     ██║███████║██████╔╝██║   ██║██║███████╗
██   ██║██╔══██║██╔══██╗╚██╗ ██╔╝██║╚════██║
╚█████╔╝██║  ██║██║  ██║ ╚████╔╝ ██║███████║
 ╚════╝ ╚═╝  ╚═╝╚═╝  ╚═╝  ╚═══╝  ╚═╝╚══════╝   IRON MAN MODE
""")
    print(f"  HUD:  http://localhost:{PORT}")
    print("  Emergency stop: " + ("press ESC in the HUD, or stop the screen sharing from the top bar.\n" if tools.IS_WAYLAND
                                  else "slam the mouse into any screen corner.\n"))
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
