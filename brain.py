"""
The brain of J.A.R.V.I.S.

* Agent loop with function calling: the LLM decides which PC tools to run, in what order.
* Works with ANY mix of providers (Gemini, Groq, OpenRouter, Mistral, GitHub Models, Ollama...).
  They form one failover chain: if a provider is overloaded / out of free quota, the next one answers,
  and the failed one is re-checked in the background so requests never wait on a dead backend.
* Vision helpers: locate a UI element on a screenshot, or describe the screen.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import platform
import re
import threading
import time
from pathlib import Path

import providers
import tools

MAX_STEPS = 25
MAX_HISTORY = 40
PROMISE_RE = re.compile(r"\b(right away|at once|on it|fabricat\w*|building|creating|opening|launching|let me|i'll|i will|one moment|working on|coming up|immediately)\b", re.I)
# tools whose repetition the user would notice (opening twice, building twice...)
ONCE_PER_REQUEST = {"open_application", "open_website", "google_search", "play_youtube", "build_web_page",
                    "present_page", "customize_interface", "type_text", "draw_with_mouse", "write_file",
                    "run_command", "media_control", "window_control", "look_through_camera"}
FATAL_RE = re.compile(r"\b(401|403)\b|invalid api key|api key not valid|API_KEY_INVALID|unauthorized|authentication", re.I)


def _parse_vision_json(txt: str) -> dict:
    """Models return {...}, [{...}], fenced code, or slightly broken JSON. Get answer + objects out of any of it."""
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", (txt or "").strip())
    data = None
    for cand in (t, (re.search(r"[\[{][\s\S]*[\]}]", t) or [None])[0]):
        if not cand:
            continue
        try:
            data = json.loads(cand)
            break
        except Exception:
            continue
    if isinstance(data, list):   # [{"answer":..., "objects": [...]}]  or  [{"label":..., "box_2d":...}, ...]
        if data and isinstance(data[0], dict) and "answer" in data[0]:
            data = data[0]
        else:
            data = {"answer": "", "objects": [d for d in data if isinstance(d, dict)]}
    if not isinstance(data, dict):   # last resort: regex the pieces out of broken JSON
        ans = re.search(r'"answer"\s*:\s*"((?:[^"\\]|\\.)*)"', t)
        objs = [{"label": m.group(1), "box_2d": [float(v) for v in re.findall(r"-?\d+(?:\.\d+)?", m.group(2))]}
                for m in re.finditer(r'"label"\s*:\s*"([^"]*)"[^{}]*?"box_2d"\s*:\s*\[([^\]]*)\]', t)]
        data = {"answer": ans.group(1) if ans else t[:600], "objects": objs}
    return data


def _similar(a: str, b: str) -> bool:
    """Rough check: does sentence a say basically the same thing as b?"""
    wa, wb = set(re.findall(r"\w+", a.lower())), set(re.findall(r"\w+", b.lower()))
    return bool(wa and wb) and len(wa & wb) / max(1, min(len(wa), len(wb))) > 0.75


PERSONALITY_FILE = Path(__file__).resolve().parent / "personality.md"
DEFAULT_PERSONALITY = """You are J.A.R.V.I.S., a witty, helpful British AI butler on the user's computer.
Address the user as "{honorific}". Replies are spoken aloud: one to three short sentences, no markdown or emojis."""


def personality(honorific: str) -> str:
    """Read personality.md fresh every request, so participants can edit it live."""
    try:
        text = PERSONALITY_FILE.read_text(encoding="utf-8")
        text = "\n".join(l for l in text.splitlines() if not l.startswith(">"))   # drop the editing notes
    except Exception:
        text = DEFAULT_PERSONALITY
    return text.replace("{honorific}", honorific)


def situation() -> str:
    """Live facts Jarvis can riff on (late night, busy CPU, low battery...)."""
    try:
        import psutil
        now = datetime.datetime.now()
        bits = [f"local time {now:%H:%M} on a {now:%A}", f"CPU {psutil.cpu_percent(interval=None):.0f}%",
                f"RAM {psutil.virtual_memory().percent:.0f}%"]
        bat = psutil.sensors_battery()
        if bat:
            bits.append(f"battery {bat.percent:.0f}%{' charging' if bat.power_plugged else ''}")
        if now.hour >= 23 or now.hour < 5:
            bits.append("it is very late")
        return ", ".join(bits)
    except Exception:
        return ""


def system_prompt(honorific: str) -> str:
    if tools.IS_WINDOWS:
        paint = "To draw in Paint: open_application('paint'), wait 2 s, window_control('maximize'), wait 1 s, then draw_with_mouse."
    else:
        paint = ("To draw: open_application('paint') opens a drawing app if one is installed (Drawing, Pinta, KolourPaint); "
                 "if none is, say so and offer to install one or draw in the air instead (hold_button=false).")
    return personality(honorific) + f"""

# How you operate (technical rules — always follow these)

YOUR BODY: You have real control of this computer through your tools — keyboard, mouse, apps, browser,
screen vision. ACT, don't just talk. When asked to do something on the PC, do it with tools immediately,
chaining as many steps as needed (open app -> wait -> click -> type ...). Don't ask for permission for
harmless actions. Only confirm first for destructive actions (closing unsaved work, deleting things).

BUILDING THINGS (be fast and impressive):
- ANY request to build/make/create a web page, website, app, dashboard, calculator, game or UI ->
  call build_web_page ONCE with a rich, detailed description (add tasteful extra features and a slick modern
  design the user didn't ask for). It writes the full code itself, streams it on the HUD and opens it.
  NEVER type code into Notepad and never write HTML yourself in type_text.
- Other files (python scripts, text, csv, notes) -> write_file. To run them -> run_command
  (e.g. "{'python' if tools.IS_WINDOWS else 'python3'} script.py"). Use run_command for folders, listing files, git, pip,
  system info. Commands run in {tools.SHELL}{'' if tools.IS_WINDOWS else ' (Linux: never use sudo, ask the user instead)'}.
- To modify something you built: read_file, then write_file with the full new content (open_with='browser').
- ALWAYS call the tool in the SAME response as any announcement. Never reply with only "Right away" — act.

PRESENTING: only when the user ASKS ("present it", "scroll through it and explain", "walk me through it")
  -> call present_page ONCE. After building something, don't present it unless asked; offer instead.
  It scrolls section by section, points with the mouse and speaks the narration itself, in sync.
  Afterwards reply with ONE short closing sentence — never re-explain what was already narrated.

YOUR EYES (WEBCAM): anything about the user or their surroundings ("what am I holding", "look at me",
  "what do you see", "how many fingers", "read this", "what am I wearing") -> look_through_camera, then answer
  in character from what it reports (a joke is welcome). Be kind about appearance. Screen questions -> look_at_screen.

YOUR OWN INTERFACE: "change your color to red", "go Iron Man mode", "turn green", "minimal mode" ->
  customize_interface. It recolors your whole HUD and on-screen effects live.

NEVER DO THINGS TWICE: each action happens exactly once per request. Never call the same tool with the same
  arguments twice, never reopen something you already opened, and never repeat a sentence you already said.

TIPS:
- After opening an app or website, use `wait` (1-2 s) before interacting with it.
- To interact with anything visible, use click_on_screen with a clear visual description.
- If unsure what is on screen or whether something worked, use look_at_screen.
- For music or videos, use play_youtube.
- {paint}
- When asked to "show off" or "show what you can do", put on a show: e.g. draw an arc_reactor in the air
  with the cursor (hold_button=false), report system diagnostics, and make a witty remark.
- You may add a very short spoken line (a quick quip is welcome) but ONLY together with the tool call in the same response.

Context: {tools.os_name()}, screen {tools.SCREEN_W}x{tools.SCREEN_H}, machine "{platform.node()}".
Each user message starts with a [live status] tag (time, CPU, battery...) you can use for context or humour.
"""


class Brain:
    def __init__(self, honorific: str = "sir", on_switch=None, on_status=None):
        self.backends = providers.discover()
        if not self.backends:
            raise RuntimeError("No AI provider configured. Put at least one API key in jarvis_ironman/.env")
        self.honorific = honorific
        self.history: list[dict] = []
        self.tool_schemas = tools.tool_schemas()
        self._down: set[str] = set()      # labels currently failing (re-checked in background)
        self._dead: set[str] = set()      # labels with bad keys (never retried)
        self._probing: set[str] = set()
        self.active = self.backends[0].label
        self.on_switch = on_switch        # callback(label) when a different backend starts answering
        self.on_status = on_status        # callback(list) whenever a backend goes up/down
        print("[brain] failover chain:\n    " + "\n    ".join(
            f"{i + 1}. {b.label}{'  (+vision)' if b.vision_ok else ''}" for i, b in enumerate(self.backends)))
        threading.Thread(target=self.warm_up, daemon=True).start()

    def warm_up(self) -> None:
        """Ping every backend in parallel at startup, so the first real request skips dead ones."""
        def check(b):
            try:
                b.ping()
            except Exception as exc:
                self._fail(b, exc)
        threads = [threading.Thread(target=check, args=(b,), daemon=True) for b in self.backends]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=35)
        ok = [b.label for b in self.backends if b.label not in self._down and b.label not in self._dead]
        if ok and ok[0] != self.active:
            self.active = ok[0]
            if self.on_switch:
                self.on_switch(self.active)
        self._status_changed()
        print(f"[brain] warm-up done: {len(ok)}/{len(self.backends)} cores ready" + (f", primary = {ok[0]}" if ok else ""))
        if not ok:
            print("\n  [!] None of your AI keys work right now (wrong key, or free quota used up for today).\n"
                  "      Open .env, check the key, or add another free one (Groq is the most generous).\n"
                  f"      See README.md -> 'Free AI keys'. Then restart {'start.bat' if tools.IS_WINDOWS else 'start.sh'}.\n")

    @property
    def model(self) -> str:
        return self.active

    def core_status(self) -> list[dict]:
        out = []
        for b in self.backends:
            state = "dead" if b.label in self._dead else "down" if b.label in self._down else "ready"
            out.append({"label": b.label, "state": state, "vision": b.vision_ok, "active": b.label == self.active})
        return out

    def _status_changed(self) -> None:
        if self.on_status:
            try:
                self.on_status(self.core_status())
            except Exception:
                pass

    # ------------------------------------------------------------ failover core
    def _chain(self, need_vision=False):
        pool = [b for b in self.backends if b.label not in self._dead and (b.vision_ok or not need_vision)]
        healthy = [b for b in pool if b.label not in self._down]
        return healthy + [b for b in pool if b not in healthy]   # known-down ones only as a last resort

    def _use(self, fn, need_vision=False, what="request"):
        last = None
        chain = self._chain(need_vision)
        if not chain:
            raise RuntimeError("No backend available" + (" with vision" if need_vision else ""))
        for b in chain:
            try:
                out = fn(b)
                if b.label in self._down:
                    self._down.discard(b.label)
                    self._status_changed()
                if what == "chat" and b.label != self.active:
                    self.active = b.label
                    print(f"[brain] now answering with {b.label}")
                    if self.on_switch:
                        self.on_switch(b.label)
                    self._status_changed()
                return out
            except providers.NoVision:
                continue
            except Exception as exc:
                last = exc
                self._fail(b, exc)
        raise last or RuntimeError("all backends failed")

    def _fail(self, b, exc) -> None:
        msg = str(exc)
        if FATAL_RE.search(msg):
            self._dead.add(b.label)
            print(f"[brain] {b.label}: bad API key / no access - disabled ({msg[:70]})")
            self._status_changed()
            return
        print(f"[brain] {b.label} unavailable ({msg[:60]}...), trying next")
        self._down.add(b.label)
        self._status_changed()
        if b.label not in self._probing:
            self._probing.add(b.label)
            threading.Thread(target=self._probe, args=(b,), daemon=True).start()

    def _probe(self, b) -> None:
        """Ping a down backend every 45 s; restore it as soon as it answers."""
        while b.label in self._down:
            time.sleep(45)
            try:
                b.ping()
                self._down.discard(b.label)
                print(f"[brain] {b.label} is back online")
                self._status_changed()
            except Exception:
                pass
        self._probing.discard(b.label)

    # ------------------------------------------------------------ code generation (streamed)
    def write_code(self, description: str, on_chunk=None) -> str:
        prompt = (
            "Create a complete, production-quality, single-file HTML document (inline CSS and JS, no external "
            "files except optional Google Fonts or CDN libraries) for:\n\n" + description + "\n\n"
            "Make it visually stunning and modern: polished layout, nice typography, gradients, smooth "
            "animations, hover effects, responsive. It must work immediately when opened from disk. "
            "STRUCTURE: put the content in clear top-level <section> elements, one per topic, each starting with "
            "a heading. For a presentation / slides / deck: every slide is a full-viewport <section class=\"slide\"> "
            "stacked vertically (scroll-snap), all slides visible in the page flow, never hidden with display:none, "
            "with big typography, a slide number, and rich visuals (CSS shapes, gradients, icons, charts). "
            "Output ONLY the raw HTML starting with <!DOCTYPE html>. No markdown fences, no explanations."
        )

        def gen(b):
            parts = []
            try:
                for t in b.stream(prompt):
                    parts.append(t)
                    if on_chunk:
                        on_chunk(t)
            except Exception:
                if on_chunk and parts:
                    on_chunk("\n\n/* ... switching neural core, retrying ... */\n\n")
                raise
            code = "".join(parts).strip()
            code = re.sub(r"^[\s\S]*?(?=<!DOCTYPE|<html)", "", code, count=1, flags=re.I)  # drop any preamble
            code = re.sub(r"\s*```\s*$", "", code)
            if "<html" not in code.lower():
                raise RuntimeError("model returned no HTML")
            return code

        return self._use(gen, what="code")

    # ------------------------------------------------------------ narration (present_page)
    def narrate(self, title: str, sections: list[dict], focus: str = "") -> list[str]:
        listing = "\n".join(f"{i + 1}. [{s.get('heading', '')}] {s.get('text', '')[:500]}" for i, s in enumerate(sections))
        prompt = (
            f"You are J.A.R.V.I.S., presenting the web page \"{title}\" (which you built) to a live audience, "
            "exactly like a charismatic human presenter walking through it on stage. The page will be scrolled "
            "section by section while you talk. For EACH section below, write what you SAY while it is on screen: "
            "1 to 3 natural spoken sentences. Explain what the section shows and point out a feature or design "
            "choice, with smooth transitions (\"Next...\", \"Now, down here...\"). Open the first one with a hook, "
            "close the last one with a confident wrap-up. Do not read the text word for word. No markdown, no emojis."
            + (f" Focus: {focus}." if focus else "")
            + f"\n\nSECTIONS:\n{listing}\n\nReturn ONLY a JSON array of exactly {len(sections)} strings."
        )
        try:
            txt = self._use(lambda b: "".join(b.stream(prompt, 4000)), what="text")
            arr = json.loads(re.search(r"\[[\s\S]*\]", txt).group(0))
            lines = [str(x).strip() for x in arr if str(x).strip()]
        except Exception as exc:
            print(f"[brain] narration fallback ({str(exc)[:60]})")
            lines = []
        while len(lines) < len(sections):
            s = sections[len(lines)]
            lines.append(f"Here we have {s.get('heading') or 'the next section'}.")
        return lines[: len(sections)]

    # ------------------------------------------------------------ vision (sync, used inside tools)
    def locate(self, jpeg: bytes, target: str) -> dict:
        prompt = (
            f'Find this UI element on the screenshot: "{target}".\n'
            'Return JSON only: {"found": true|false, "point": [y, x]} where the point is the CENTER of the '
            "element, normalized to 0-1000 (y first). If it is not visible, found=false."
        )
        txt = self._use(lambda b: b.vision(jpeg, prompt, json_mode=True), need_vision=True)
        try:
            data = json.loads(re.search(r"[\[{][\s\S]*[\]}]", txt).group(0))
            if isinstance(data, list):
                data = data[0] if data else {}
        except Exception:
            nums = re.findall(r"\d+(?:\.\d+)?", txt)
            data = {"found": len(nums) >= 2, "point": [float(nums[0]), float(nums[1])] if len(nums) >= 2 else None}
        pt = data.get("point")
        if not data.get("found") or not pt or len(pt) < 2:
            return {"found": False}
        return {"found": True, "point": [float(pt[0]), float(pt[1])]}

    def camera_vision(self, jpeg: bytes, question: str) -> dict:
        """Look at a webcam photo: a factual answer + boxes around the relevant objects (for the HUD)."""
        prompt = (
            f'This is a live webcam photo of the user, taken just now. The user asks: "{question}"\n'
            "Return JSON only, no markdown:\n"
            '{"answer": "<a precise, factual answer: what is visible, what they are holding/wearing/doing; '
            'read any visible text or logos>", '
            '"objects": [{"label": "<1-3 word name>", "box_2d": [ymin, xmin, ymax, xmax]}]}\n'
            "objects = up to 5 things most relevant to the question (include the held item first if any), "
            "box coordinates normalized to 0-1000. Be kind and neutral about people's appearance."
        )
        txt = self._use(lambda b: b.vision(jpeg, prompt, json_mode=True), need_vision=True)
        data = _parse_vision_json(txt)
        objs = []
        for o in data.get("objects") or []:
            box = o.get("box_2d") or o.get("box")
            if isinstance(box, list) and len(box) == 4 and all(isinstance(v, (int, float)) for v in box):
                y0, x0, y1, x1 = (max(0.0, min(1000.0, float(v))) / 1000 for v in box)
                if y1 > y0 and x1 > x0:
                    objs.append({"label": str(o.get("label", ""))[:30], "box": [x0, y0, x1, y1]})
        return {"answer": str(data.get("answer", "")).strip(), "objects": objs[:5]}

    def describe(self, jpeg: bytes, question: str) -> str:
        prompt = f"This is a screenshot of the user's screen. {question}\nAnswer concisely and factually."
        return self._use(lambda b: b.vision(jpeg, prompt), need_vision=True).strip()

    # ------------------------------------------------------------ agent loop
    def reset(self) -> None:
        self.history.clear()

    def _trim(self) -> None:
        if len(self.history) <= MAX_HISTORY:
            return
        for i in range(len(self.history) - MAX_HISTORY, len(self.history)):
            if self.history[i]["role"] == "user":
                self.history = self.history[i:]
                return

    async def run(self, text: str, ctx, say) -> str:
        """Run one user request to completion. `say(text)` speaks interim lines."""
        self._trim()
        # live facts go in the message (not the system prompt) so the big system prompt stays identical
        # between requests -> providers and local models can cache it -> much faster answers
        self.history.append({"role": "user", "content": f"[{tools.now_string()} | {situation()}] {text}"})
        system = system_prompt(self.honorific)
        acted = nudged = False
        done_calls: dict[str, str] = {}      # "name+args" -> result json (this request)
        done_names: set[str] = set()
        spoken: list[str] = []
        for _ in range(MAX_STEPS):
            if getattr(ctx, "interrupted", None) is not None and ctx.interrupted.is_set():
                ctx.overlay.control(False)
                return ""
            out = await asyncio.to_thread(
                self._use, lambda b: b.chat(self.history, system, self.tool_schemas), False, "chat")
            texts, calls = out["text"], out["calls"]
            self.history.append({"role": "assistant", "content": texts, "calls": calls})
            if not calls and not nudged and PROMISE_RE.search(texts or "") and len(texts) < 160:
                # The model announced an action but didn't do it (common with small models) -> push it to act.
                nudged = True
                if texts:
                    asyncio.create_task(say(texts))
                self.history.append({"role": "user", "content":
                                     "(system) You announced an action but did not call any tool. Call the needed tool(s) NOW."})
                continue
            if not calls:
                if acted:
                    ctx.overlay.control(False)
                if texts and any(_similar(texts, s) for s in spoken):
                    return ""                      # already said it -> don't say it twice
                return texts or ("" if spoken else "Done.")
            presenting = any(c["name"] == "present_page" for c in calls)   # it narrates itself
            if texts and not presenting and not any(_similar(texts, s) for s in spoken):
                spoken.append(texts)
                asyncio.create_task(say(texts))   # speak while we keep working
            if not acted:
                acted = True
                ctx.overlay.control(True)
            seen_here = set()
            for c in calls:
                key = c["name"] + json.dumps(c["args"], sort_keys=True, default=str)
                if key in seen_here or key in done_calls or (c["name"] in ONCE_PER_REQUEST and c["name"] in done_names
                                                              and c["name"] in ("build_web_page", "present_page", "play_youtube",
                                                                                "look_through_camera")):
                    print(f"[tool] skipped duplicate {c['name']}")
                    self.history.append({"role": "tool", "id": c["id"], "name": c["name"], "content": json.dumps(
                        {"ok": True, "note": "ALREADY DONE earlier in this request - do not repeat it. Continue or finish."})})
                    continue
                seen_here.add(key)
                print(f"[tool] {c['name']} {str(c['args'])[:120]}")
                ctx.emit({"type": "tool", "name": c["name"], "args": c["args"]})
                result = await asyncio.to_thread(tools.execute, ctx, c["name"], c["args"])
                ok = not (isinstance(result, dict) and result.get("ok") is False)
                ctx.emit({"type": "tool_result", "name": c["name"], "ok": ok,
                          "summary": json.dumps(result, default=str)[:220]})
                if ok:
                    done_calls[key] = "done"
                    done_names.add(c["name"])
                payload = json.dumps(result, default=str)
                if len(payload) > 8000:
                    payload = json.dumps({"truncated_result": payload[:8000]})
                self.history.append({"role": "tool", "id": c["id"], "name": c["name"], "content": payload})
        ctx.overlay.control(False)
        return "That took more steps than I'd like. I've stopped here — shall I continue?"
