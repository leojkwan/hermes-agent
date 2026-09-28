#!/usr/bin/env python3
"""Real GPT-Live A/B of the voice-layer persona in ``tools/voice_live.py``.

Speaks one scripted, synthetic conversation into two live GPT-Live sessions -- the persona at a
base git revision against the working tree's -- and counts what the voice layer does with each
turn: which turns become delegations, which replies are only a "checking" line, and how often
each backend result is spoken.

Why audio over a WebSocket: in client delegation the live model takes user input as audio only
(there is no text-turn event), so each turn is macOS ``say`` speech streamed at real-time pace.
Between turns the stream carries a faint noise floor, never silence: the model injects appended
context only while input audio keeps flowing. The session config comes from the module's own
``build_session_config()``; the WebSocket transport only adds ``audio.format`` (WebRTC
negotiates it).

The backend is simulated the way the voice page drives it: on a delegation it appends the page's
quiet ``session.thinking.append`` when no other task is running and, after a delay, one canned
result through ``session.commentary.append``. No Hermes service is contacted.

Both arms get the same ``--extra-instructions`` (e.g. the user's ``voice.gpt_live.instructions``),
appended exactly as ``live_instructions()`` appends them in production.

Cost: $0.05 per minute of session time on ``OPENAI_API_KEY``. Each arm is capped by ``--cap``.

  python scripts/voice_live_persona_ab.py --out DIR --base REV [--extra-instructions FILE]
"""

from __future__ import annotations

import argparse
import array
import asyncio
import base64
import collections
import hashlib
import json
import os
import random
import re
import subprocess
import sys
import types
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from websockets.asyncio.client import connect

ROOT = Path(__file__).resolve().parent.parent
LIVE_URL = "wss://api.openai.com/v1/live/sessions"
RATE = 24_000
FRAME_MS = 20
FRAME_BYTES = RATE * FRAME_MS // 1000 * 2
# What the voice page appends when it starts a foreground Hermes turn (index.html launchTurn).
PAGE_THINKING = "On it — Hermes is checking now."


@dataclass(frozen=True)
class Turn:
    kind: str  # ask, fix: substantive. ack, aside, think, status: conversational.
    text: str
    result: str = "Got it."  # what the simulated backend returns if this turn is delegated
    token: str = ""  # regex that marks the result being spoken
    delay: float = 12.0  # seconds from delegation to result

    @property
    def substantive(self) -> bool:
        return self.kind in ("ask", "fix")


# Synthetic lines modeled on the failure categories (reactions, asides to someone in the room,
# thinking aloud, status questions about a running task) around six real asks. The first ask's
# result is held for 75 s so the status questions have a genuinely outstanding task.
SCRIPT = (
    Turn("ask", "Can you look up what time the hardware store on Example Avenue closes today?",
         "The hardware store on Example Avenue closes at eight fifteen tonight.",
         r"eight[\s-]*fifteen|8[:.]15|quarter past eight", 75),
    Turn("ack", "Okay, cool."),
    Turn("think", "Hmm, I think I left the good drill at my brother's place anyway."),
    Turn("status", "Any luck with that yet?"),
    Turn("aside", "Sorry, hang on, my roommate is asking me something."),
    Turn("ack", "Ha, fair enough."),
    Turn("ask", "Add eggs, oat milk and coffee filters to my shopping list.",
         "Done. That brings the shopping list to eleven items.", r"\beleven\b|\b11\b", 20),
    Turn("status", "Is the store thing still going?"),
    Turn("think", "Let me think, what else do we need for the weekend."),
    Turn("ask", "What did I decide last week about moving the dentist appointment?",
         "Last week you decided to move the dentist appointment to Thursday the ninth at four.",
         r"\bninth\b|\b9th\b|thursday", 20),
    Turn("aside", "No, the blue one, it's on the counter by the fridge."),
    Turn("ack", "Nice, okay."),
    Turn("fix", "Actually, I meant the hardware store on Sample Street, not Example Avenue.",
         "The Sample Street hardware store closes earlier, at six forty-five.",
         r"six[\s-]*forty[\s-]*five|6[:.]45|quarter to seven", 20),
    Turn("think", "Okay, so if I leave by six I'm fine."),
    Turn("status", "Did that come back yet?"),
    Turn("ask", "Set a reminder at six to take the chicken out of the freezer.",
         "Reminder set for six p.m. Heads up, the chicken needs about eight hours to thaw.",
         r"eight hours|8 hours", 20),
    Turn("aside", "Sorry, one sec, I'm talking to my neighbor."),
    Turn("ack", "Great, thanks."),
    Turn("ask", "Find me a banana bread recipe that doesn't use eggs.",
         "Found one that swaps each egg for a quarter cup of applesauce and bakes for fifty-five minutes.",
         r"applesauce", 20),
    Turn("ack", "Okay, that sounds easy enough."),
)

# A bare checking line says only that work is happening ("Checkin'.", "Sure. Checking with Hermes.",
# "Not yet, still pending."): every word is hold vocabulary and one of them is a checking/status word.
_CHECK_WORDS = {"checking", "checkin", "check", "looking", "look", "pending", "progress", "running",
                "working", "waiting"}
_HOLD_WORDS = _CHECK_WORDS | set(
    "okay ok sure alright all right yeah yep yes got it mm hmm oh so not yet still into on that thats "
    "this now for you real quick up one sec second just a give me let lemme i ill im will get have ask "
    "hermes he hes is its we were are am to with in the moment bit back shortly soon be there".split())


def _normalize(text: str) -> str:
    text = text.lower().replace("’", "'").replace("'", "")
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", text).split())


def is_bare_hold(text: str) -> bool:
    words = _normalize(text).split()
    return (0 < len(words) <= 10 and set(words) <= _HOLD_WORDS
            and bool(_CHECK_WORDS & set(words) or " on it" in f" {' '.join(words)}"))


def load_voice_live(rev: Optional[str]) -> types.ModuleType:
    """``tools/voice_live.py`` at ``rev`` (via git) or from the working tree when ``rev`` is None."""
    if rev is None:
        source, name = (ROOT / "tools" / "voice_live.py").read_text(encoding="utf-8"), "voice_live_worktree"
    else:
        source = subprocess.run(
            ["git", "-C", str(ROOT), "show", f"{rev}:tools/voice_live.py"],
            check=True, capture_output=True, text=True, stdin=subprocess.DEVNULL).stdout
        name = "voice_live_" + re.sub(r"\W", "_", rev)
    module = types.ModuleType(name)
    exec(compile(source, f"{name}.py", "exec"), module.__dict__)  # noqa: S102 - our own source
    return module


def session_config(module: types.ModuleType, extra: str) -> Dict[str, Any]:
    """The production builder with a fixed config section (no read of any installed config)."""
    live = {"instructions": extra} if extra else {}
    module._live_section = lambda voice=None: live
    config = module.build_session_config()
    config["audio"]["format"] = {"type": "audio/pcm", "rate": RATE}
    return config


def synthesize(text: str, cache: Path, voice: str) -> bytes:
    cache.mkdir(parents=True, exist_ok=True)
    stem = hashlib.sha256(f"{voice}|{text}".encode()).hexdigest()[:16]
    wav_path = cache / f"{stem}.wav"
    if not wav_path.exists():
        aiff = cache / f"{stem}.aiff"
        subprocess.run(["say", "-v", voice, "-r", "180", "-o", str(aiff), text],
                       check=True, stdin=subprocess.DEVNULL)
        subprocess.run(["afconvert", "-f", "WAVE", "-d", f"LEI16@{RATE}", "-c", "1", str(aiff), str(wav_path)],
                       check=True, stdin=subprocess.DEVNULL)
        aiff.unlink()
    with wave.open(str(wav_path)) as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, RATE):
            raise RuntimeError(f"{wav_path} is not mono PCM16 at {RATE} Hz")
        return wav.readframes(wav.getnframes())


def _redact_instructions(value: Any) -> Any:
    """Logs carry a hash of the instructions (they may include the user's own), not the text."""
    if isinstance(value, dict):
        return {key: (f"<sha256:{hashlib.sha256(item.encode()).hexdigest()[:12]} chars:{len(item)}>"
                      if key == "instructions" and isinstance(item, str) else _redact_instructions(item))
                for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_instructions(item) for item in value]
    return value


def _noise_frames(seed: int, count: int = 64) -> List[bytes]:
    """Faint room noise (~-67 dBFS): keeps input audio flowing without reading as speech."""
    rng = random.Random(seed)
    return [array.array("h", (rng.randint(-26, 26) for _ in range(FRAME_BYTES // 2))).tobytes()
            for _ in range(count)]


class Arm:
    """One live session speaking the script, with the voice page's delegation side effects."""

    def __init__(self, name: str, config: Dict[str, Any], script, pcm: List[bytes], cap_s: float,
                 log_path: Path):
        self.name, self.config, self.script, self.pcm, self.cap_s = name, config, script, pcm, cap_s
        self.log_path, self.log_file = log_path, None
        self.ws = None
        self.samples = 0  # input samples sent: the session timeline in our clock
        self.noise = _noise_frames(seed=len(name))
        self.frames: collections.deque = collections.deque()
        self.frames_done: Optional[asyncio.Event] = None
        self.started, self.closed = asyncio.Event(), asyncio.Event()
        self.stop_audio = False
        self.turn = -1
        self.turn_marks: List[Dict[str, Any]] = []
        self.outputs: List[Dict[str, Any]] = []
        self.inputs: List[Dict[str, Any]] = []
        self.delegations: List[Dict[str, Any]] = []
        self.appends: List[Dict[str, Any]] = []
        self.errors: List[Dict[str, Any]] = []
        self.foreground: Optional[str] = None
        self.outstanding = 0
        self.session_id: Optional[str] = None
        self.usage_seconds: Optional[float] = None
        self.close_reason: Optional[str] = None
        self.tasks: List[asyncio.Task] = []

    def now_ms(self) -> int:
        return self.samples * 1000 // RATE

    def log(self, record: Dict[str, Any]) -> None:
        if self.log_file is None:
            self.log_file = self.log_path.open("w", encoding="utf-8")
        record = _redact_instructions({"at_ms": self.now_ms(), **record})
        self.log_file.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.log_file.flush()

    @classmethod
    def from_log(cls, name: str, log_path: Path, script) -> "Arm":
        """Rebuild what ``score`` reads from a run's JSONL log: re-score without a new session.
        Events belong to the turn whose speech had started, as in the live run."""
        records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        arm = cls(name, {}, script, [], 0.0, log_path)
        arm.turn_marks = [{key: r[key] for key in ("turn", "kind", "text", "start_ms", "end_ms")}
                          for r in records if r.get("type") == "harness.turn"]

        def turn_at(ms: int) -> int:
            return max((m["turn"] for m in arm.turn_marks if m["start_ms"] <= ms), default=-1)

        for r in records:
            kind, at = r.get("type"), r["at_ms"]
            if r.get("dir") == "client" and kind in ("session.thinking.append", "session.commentary.append"):
                turn = next((d["turn"] for d in arm.delegations if d["id"] == r.get("delegation_id")), -1)
                arm.appends.append({"kind": kind, "delegation_id": r.get("delegation_id"), "at_ms": at,
                                    "turn": turn if kind == "session.commentary.append" else -1,
                                    "event_id": r.get("event_id")})
            if r.get("dir") != "server":
                continue
            if kind == "session.started":
                arm.session_id = (r.get("session") or {}).get("id")
            elif kind == "session.output_transcript.delta":
                arm.outputs.append({"at_ms": at, "turn": turn_at(at), "text": r.get("delta", "")})
            elif kind == "session.input_transcript.delta":
                arm.inputs.append({"at_ms": at, "turn": turn_at(at), "text": r.get("delta", "")})
            elif kind == "session.delegation.created":
                arm.delegations.append({"id": (r.get("delegation") or {}).get("id"), "turn": turn_at(at),
                                        "at_ms": at, "offset_ms": r.get("offset_ms")})
            elif kind in ("session.usage.updated", "session.closed"):
                arm.usage_seconds = (r.get("usage") or {}).get("seconds", arm.usage_seconds)
                arm.close_reason = r.get("reason", arm.close_reason)
            elif kind == "error":
                arm.errors.append(r.get("error") or {})
        return arm

    async def send(self, event: Dict[str, Any]) -> None:
        if event.get("type") != "session.input_audio.append":
            self.log({"dir": "client", **event})
        await self.ws.send(json.dumps(event))

    async def pump(self) -> None:
        """Input audio at real-time pace: queued speech frames, else the noise floor."""
        loop = asyncio.get_running_loop()
        next_at, index = loop.time(), 0
        while not self.stop_audio:
            if self.frames:
                frame = self.frames.popleft()
            else:
                frame, index = self.noise[index % len(self.noise)], index + 1
            await self.ws.send(json.dumps({"type": "session.input_audio.append",
                                           "audio": base64.b64encode(frame).decode("ascii")}))
            self.samples += len(frame) // 2
            if not self.frames and self.frames_done is not None:
                self.frames_done.set()
                self.frames_done = None
            next_at += FRAME_MS / 1000
            await asyncio.sleep(max(0.0, next_at - loop.time()))

    async def speak(self, pcm: bytes) -> tuple:
        frames = [pcm[i:i + FRAME_BYTES] for i in range(0, len(pcm), FRAME_BYTES)]
        frames[-1] += self.noise[0][:FRAME_BYTES - len(frames[-1])]
        done = asyncio.Event()
        start = self.now_ms()
        self.frames_done = done
        self.frames.extend(frames)
        await done.wait()
        return start, self.now_ms()

    async def receive(self) -> None:
        async for raw in self.ws:
            event = json.loads(raw)
            kind = event.get("type", "")
            if kind == "session.output_audio.delta":
                continue  # audio itself is not logged; its transcript is
            self.log({"dir": "server", **event})
            if kind == "session.started":
                self.session_id = (event.get("session") or {}).get("id")
                self.started.set()
            elif kind == "session.output_transcript.delta":
                self.outputs.append({"at_ms": self.now_ms(), "turn": self.turn, "text": event.get("delta", ""),
                                     "start_ms": event.get("start_ms"), "end_ms": event.get("end_ms")})
            elif kind == "session.input_transcript.delta":
                self.inputs.append({"at_ms": self.now_ms(), "turn": self.turn, "text": event.get("delta", "")})
            elif kind == "session.delegation.created":
                self.on_delegation(event)
            elif kind == "session.usage.updated":
                self.usage_seconds = (event.get("usage") or {}).get("seconds", self.usage_seconds)
            elif kind == "error":
                self.errors.append(event.get("error") or {})
            elif kind == "session.closed":
                self.usage_seconds = (event.get("usage") or {}).get("seconds", self.usage_seconds)
                self.close_reason = event.get("reason")
                self.closed.set()
                return

    def on_delegation(self, event: Dict[str, Any]) -> None:
        delegation_id = (event.get("delegation") or {}).get("id")
        turn = self.script[self.turn] if self.turn >= 0 else None
        self.delegations.append({"id": delegation_id, "turn": self.turn, "at_ms": self.now_ms(),
                                 "offset_ms": event.get("offset_ms")})
        if self.foreground is None:  # the page's foreground hop; background hops stay quiet
            self.foreground = delegation_id
            self.tasks.append(asyncio.create_task(self.append("session.thinking.append", delegation_id, PAGE_THINKING)))
        self.outstanding += 1
        self.tasks.append(asyncio.create_task(self.deliver(delegation_id, self.turn, turn)))

    async def append(self, kind: str, delegation_id: Optional[str], content: str, turn: int = -1) -> None:
        event_id = f"{kind.split('.')[1]}_{len(self.appends) + 1}"
        self.appends.append({"kind": kind, "delegation_id": delegation_id, "turn": turn,
                             "at_ms": self.now_ms(), "event_id": event_id})
        await self.send({"type": kind, "event_id": event_id, "delegation_id": delegation_id, "content": content})

    async def deliver(self, delegation_id: str, index: int, turn: Optional[Turn]) -> None:
        try:
            await asyncio.sleep(turn.delay if turn else 12.0)
            if not self.stop_audio:
                await self.append("session.commentary.append", delegation_id, turn.result if turn else "Got it.", index)
        finally:
            if self.foreground == delegation_id:
                self.foreground = None
            self.outstanding -= 1

    async def wait_for_quiet(self, feed_end: int) -> None:
        """Next turn after >=3 s and 1.2 s without assistant transcript, or 8 s at most."""
        while True:
            now = self.now_ms()
            last_output = max((o["at_ms"] for o in self.outputs), default=0)
            if now - feed_end >= 8000 or (now - feed_end >= 3000 and now - max(last_output, feed_end) >= 1200):
                return
            await asyncio.sleep(0.1)

    async def converse(self) -> None:
        cap_ms = self.cap_s * 1000
        for index, turn in enumerate(self.script):
            if self.now_ms() > cap_ms - 25_000:
                self.log({"dir": "harness", "type": "harness.cap_skip", "turn": index})
                break
            self.turn = index
            start, end = await self.speak(self.pcm[index])
            mark = {"turn": index, "kind": turn.kind, "text": turn.text, "start_ms": start, "end_ms": end}
            self.turn_marks.append(mark)
            self.log({"dir": "harness", "type": "harness.turn", **mark})
            await self.wait_for_quiet(end)
        while self.now_ms() < cap_ms:  # tail: let outstanding results arrive and be spoken
            last_append = max((a["at_ms"] for a in self.appends), default=0)
            last_output = max((o["at_ms"] for o in self.outputs), default=0)
            now = self.now_ms()
            if not self.outstanding and now - last_append >= 10_000 and now - last_output >= 3000:
                break
            await asyncio.sleep(0.2)

    async def run(self, api_key: str) -> "Arm":
        headers = {"Authorization": f"Bearer {api_key}"}
        async with connect(LIVE_URL, additional_headers=headers, max_size=None, open_timeout=30) as ws:
            self.ws = ws
            receiver = asyncio.create_task(self.receive())
            await self.send({"type": "session.start", "event_id": "start", "session": self.config})
            started = asyncio.create_task(self.started.wait())
            await asyncio.wait({started, receiver}, timeout=30, return_when=asyncio.FIRST_COMPLETED)
            if not self.started.is_set():
                started.cancel()
                raise RuntimeError(f"{self.name}: no session.started ({self.errors or 'timeout'})")
            pump = asyncio.create_task(self.pump())
            try:
                await asyncio.wait_for(self.converse(), timeout=self.cap_s + 30)
            finally:
                self.stop_audio = True
                await pump
                await self.send({"type": "session.close", "event_id": "close"})
                try:
                    await asyncio.wait_for(self.closed.wait(), 20)
                except asyncio.TimeoutError:
                    self.log({"dir": "harness", "type": "harness.close_unconfirmed"})
                receiver.cancel()
                for task in self.tasks:
                    task.cancel()
                if self.log_file:
                    self.log_file.close()
        return self


def _utterances(outputs: List[Dict[str, Any]], gap_ms: int = 1000) -> List[Dict[str, Any]]:
    groups: List[Dict[str, Any]] = []
    for out in outputs:
        if groups and out["at_ms"] - groups[-1]["last_ms"] <= gap_ms:
            groups[-1]["text"] += out["text"]
            groups[-1]["last_ms"] = out["at_ms"]
        else:
            groups.append({"at_ms": out["at_ms"], "last_ms": out["at_ms"], "text": out["text"]})
    return groups


def score(arm: Arm) -> Dict[str, Any]:
    marks = arm.turn_marks
    per_turn = []
    for position, mark in enumerate(marks):
        turn = arm.script[mark["turn"]]
        end = marks[position + 1]["start_ms"] if position + 1 < len(marks) else float("inf")
        reply = "".join(o["text"] for o in arm.outputs if mark["start_ms"] <= o["at_ms"] < end).strip()
        heard = "".join(i["text"] for i in arm.inputs if mark["start_ms"] <= i["at_ms"] < end).strip()
        delegated = [d for d in arm.delegations if d["turn"] == mark["turn"]]
        per_turn.append({"turn": mark["turn"], "kind": turn.kind, "text": turn.text, "heard": heard,
                         "delegations": len(delegated),
                         "delegation_lag_ms": [d["at_ms"] - mark["end_ms"] for d in delegated],
                         "reply": reply, "bare_hold_reply": is_bare_hold(reply)})
    utterances = _utterances(arm.outputs)
    results = []
    for append in arm.appends:
        if append["kind"] != "session.commentary.append" or append["turn"] < 0:
            continue
        turn = arm.script[append["turn"]]
        if not turn.token:
            continue
        spoken = [u["text"].strip() for u in utterances
                  if u["last_ms"] >= append["at_ms"] and re.search(turn.token, u["text"], re.I)]
        results.append({"turn": append["turn"], "appended_at_ms": append["at_ms"], "times_spoken": len(spoken),
                        "spoken": spoken})
    conversational = [t for t in per_turn if t["kind"] not in ("ask", "fix")]
    substantive = [t for t in per_turn if t["kind"] in ("ask", "fix")]
    return {
        "arm": arm.name, "session_id": arm.session_id, "usage_seconds": arm.usage_seconds,
        "close_reason": arm.close_reason, "errors": arm.errors,
        "turns_spoken": len(marks), "delegations_total": len(arm.delegations),
        "conversational_turns": len(conversational),
        "conversational_turns_delegated": sum(1 for t in conversational if t["delegations"]),
        "conversational_delegations": sum(t["delegations"] for t in conversational),
        "substantive_turns": len(substantive),
        "substantive_turns_delegated": sum(1 for t in substantive if t["delegations"]),
        "bare_hold_replies": sum(1 for t in per_turn if t["bare_hold_reply"]),
        "bare_hold_replies_conversational": sum(1 for t in conversational if t["bare_hold_reply"]),
        "bare_hold_utterances": [u["text"].strip() for u in utterances if is_bare_hold(u["text"])],
        # the page's quiet thinking line read out verbatim: a page cost, identical in both arms
        "page_thinking_echoes": sum(1 for u in utterances if _normalize(PAGE_THINKING) in _normalize(u["text"])),
        "results": results, "turns": per_turn,
    }


def transcript(summary: Dict[str, Any]) -> str:
    lines = [f"# {summary['arm']}  session={summary['session_id']}  usage={summary['usage_seconds']}s"]
    for t in summary["turns"]:
        flag = f"  [DELEGATED x{t['delegations']}]" if t["delegations"] else ""
        lines.append(f"\n[{t['turn']:02d} {t['kind']}] USER: {t['text']}{flag}")
        lines.append(f"      heard: {t['heard']}")
        lines.append(f"      VOICE: {t['reply'] or '(silent)'}" + ("   <-- bare hold" if t["bare_hold_reply"] else ""))
    return "\n".join(lines) + "\n"


def report(out: Path, arms: List[Arm]) -> None:
    summaries = [score(arm) for arm in arms]
    for arm, summary in zip(arms, summaries):
        (out / f"{arm.name}.txt").write_text(transcript(summary), encoding="utf-8")
    (out / "summary.json").write_text(json.dumps(summaries, indent=2, ensure_ascii=False), encoding="utf-8")
    header = ("arm", "conv turns delegated", "conv delegations", "subst turns delegated", "bare-hold replies",
              "hold utterances", "page echoes", "times each result spoken", "usage s")
    print(" | ".join(header))
    for s in summaries:
        spoken = ",".join(str(r["times_spoken"]) for r in s["results"]) or "-"
        print(" | ".join(str(v) for v in (
            s["arm"], f"{s['conversational_turns_delegated']}/{s['conversational_turns']}",
            s["conversational_delegations"], f"{s['substantive_turns_delegated']}/{s['substantive_turns']}",
            s["bare_hold_replies"], len(s["bare_hold_utterances"]), s["page_thinking_echoes"], spoken,
            s["usage_seconds"])))


async def main_async(args: argparse.Namespace) -> int:
    out = Path(args.out).expanduser()
    if args.rescore:
        report(out, [Arm.from_log(name, out / f"{name}.jsonl", SCRIPT) for name in args.arms.split(",")])
        return 0
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        print("OPENAI_API_KEY is not set", file=sys.stderr)
        return 2
    out.mkdir(parents=True, exist_ok=True)
    extra = Path(args.extra_instructions).expanduser().read_text(encoding="utf-8").strip() if args.extra_instructions else ""
    script = SCRIPT[: args.turns] if args.turns else SCRIPT
    pcm = [synthesize(turn.text, out / "fixtures", args.voice) for turn in script]
    modules = {"old": lambda: load_voice_live(args.base), "new": lambda: load_voice_live(None)}
    arms = []
    for name in args.arms.split(","):
        module = modules[name]()
        config = session_config(module, extra)
        meta = {"arm": name, "rev": args.base if name == "old" else "worktree",
                "persona_sha256": hashlib.sha256(module.LIVE_PERSONA.encode()).hexdigest()[:12],
                "instructions_sha256": hashlib.sha256(config["instructions"].encode()).hexdigest()[:12],
                "instructions_chars": len(config["instructions"]), "extra_chars": len(extra),
                "model": config["model"], "voice": config["audio"]["output"]["voice"]}
        print(json.dumps(meta))
        arms.append(Arm(name, config, script, pcm, args.cap, out / f"{name}.jsonl"))
    results = await asyncio.gather(*(arm.run(api_key) for arm in arms), return_exceptions=True)
    for arm, result in zip(arms, results):
        if isinstance(result, BaseException):
            print(f"{arm.name}: FAILED {result!r}", file=sys.stderr)
    report(out, arms)
    return 0 if all(not isinstance(r, BaseException) for r in results) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", required=True, help="directory for fixtures, JSONL logs and the summary")
    parser.add_argument("--base", default="HEAD", help="git revision whose persona is the 'old' arm")
    parser.add_argument("--arms", default="old,new", help="comma list of old,new")
    parser.add_argument("--extra-instructions", help="file appended to both personas, as production does")
    parser.add_argument("--turns", type=int, default=0, help="speak only the first N turns (smoke test)")
    parser.add_argument("--cap", type=float, default=270.0, help="max session seconds per arm")
    parser.add_argument("--voice", default="Reed", help="macOS say voice for the user")
    parser.add_argument("--rescore", action="store_true", help="re-score the JSONL logs in --out; no session")
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
