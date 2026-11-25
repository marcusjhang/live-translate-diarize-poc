# /// script
# requires-python = ">=3.11"
# dependencies = ["google-genai"]
# ///
"""Multi-turn Live Translate harness: does continuous, starter-style input beat the
app's gated, manual-turn input across a conversation? (Phase 1 of
docs/starter-alignment-plan.md.)

Plays a doctor/patient conversation of recorded clips, in real time on a 100 ms grid,
into TWO live sessions (doctor->patient language, patient->doctor language) and
scores every clip: did the model hear all of it, did the last words survive, how
fast, did output bleed into the next turn, did a session translate the wrong
speaker or its own playback.

Input modes
  --input gated       today's app: manual VAD + activity_start/end, warm-up turn,
                      speech + 1.2 s mic tail, then nothing, then 800 ms zeros +
                      activity_end; nothing between turns. Routed only.
  --input continuous  the starter: auto VAD, a 100 ms chunk every 100 ms for the
                      whole run (clip audio, or the mic noise floor between clips).

Session modes (continuous only)
  --sessions routed   option A: mic goes to the current speaker's session (stays on
                      the last speaker during gaps); the other gets --idle.
  --sessions both     option B: mic goes to both sessions always.

  --idle noise|zero|nothing   what the non-routed session receives in option A
  --floor RMS                 mic noise floor between clips (0 = digital zero)
  --feedback                  mix translated playback back into the mic at -20 dB
  --suppress                  while session S's output plays, the other session
                              gets the noise floor instead of the mic

  uv run experiments/multiturn.py --input continuous --sessions both --gap 5
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_lib as H  # noqa: E402
sys.path.insert(0, str(H.ROOT))
from prompt import system_instruction  # noqa: E402

from google import genai  # noqa: E402
from google.genai import types  # noqa: E402

MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-live-translate-preview")
AUDIO_RMS_MIN = 220  # same loud/padding split the app uses for playback
MIME = "audio/pcm;rate=16000"

PRESETS = {
    "conv": ["turn-d1", "turn-p1", "turn-d2", "turn-p2", "turn-d3",
             "turn-p3", "turn-d4", "turn-p4", "turn-d5", "turn-p5"],
    "conv4": ["turn-d1", "turn-p1", "turn-d2", "turn-p2"],
    "hard": ["s11-d1", "c-p1", "s07-d1", "s08-p1", "b13-d1", "turn-p4"],
}
TARGET = {"doctor": "zh-Hans", "patient": "en"}  # doctor speaks en, patient zh
SOURCE = {"doctor": "en", "patient": "zh"}
INSTRUCTION = False  # --instruction on: the app's per-direction system instruction


def load_dotenv():
    env = H.ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


def read_wav(path: Path) -> bytes:
    with wave.open(str(path)) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2), path
        return w.readframes(w.getnframes())


def make_client(backend: str):
    if backend == "enterprise":
        return genai.Client(enterprise=True, project=os.environ["GOOGLE_CLOUD_PROJECT"],
                            location=os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1"))
    return genai.Client(api_key=os.environ["GEMINI_API_KEY"],
                        http_options={"api_version": os.environ.get("GEMINI_API_VERSION", "v1beta")})


def make_config(target: str, gated: bool, handle: str | None, instruction: str | None = None):
    cfg = dict(
        system_instruction=instruction,
        response_modalities=[types.Modality.AUDIO],
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        translation_config=types.TranslationConfig(target_language_code=target,
                                                   echo_target_language=False),
        session_resumption=types.SessionResumptionConfig(handle=handle) if handle
        else types.SessionResumptionConfig(),
    )
    if gated:  # exactly the app's current config
        cfg["realtime_input_config"] = types.RealtimeInputConfig(
            automatic_activity_detection=types.AutomaticActivityDetection(disabled=True))
        cfg["context_window_compression"] = types.ContextWindowCompressionConfig(
            sliding_window=types.SlidingWindow(target_tokens=16384))
    return types.LiveConnectConfig(**cfg)


class Side:
    """One translation direction: a live session that reconnects with its handle."""

    def __init__(self, name, client, gated, events, clock, on_audio):
        self.name, self.client, self.gated = name, client, gated
        self.events, self.clock, self.on_audio = events, clock, on_audio
        self.session = None
        self.handle = None
        self.ready = asyncio.Event()
        self.stop = False
        self.task = None

    def ev(self, kind, text="", loud=False):
        self.events.append(H.Event(round(self.clock(), 2), self.name, kind, text, loud))

    async def run(self):
        first = True
        while not self.stop:
            try:
                inst = (system_instruction(self.name, SOURCE[self.name], SOURCE["patient" if self.name == "doctor" else "doctor"])
                        if INSTRUCTION else None)
                cfg = make_config(TARGET[self.name], self.gated, self.handle, inst)
                async with self.client.aio.live.connect(model=MODEL, config=cfg) as s:
                    if self.gated and first:  # app's warm-up turn
                        await s.send_realtime_input(activity_start=types.ActivityStart())
                        for _ in range(4):
                            await s.send_realtime_input(audio=types.Blob(data=b"\0" * H.CHUNK_BYTES, mime_type=MIME))
                            await asyncio.sleep(0.1)
                        await s.send_realtime_input(activity_end=types.ActivityEnd())
                    if not first:
                        self.ev("reconnect")
                    first = False
                    self.session = s
                    self.ready.set()
                    async for r in s.receive():
                        if r.session_resumption_update and r.session_resumption_update.new_handle:
                            self.handle = r.session_resumption_update.new_handle
                        if r.go_away:
                            self.ev("go_away")
                            break
                        sc = r.server_content
                        if not sc:
                            continue
                        if sc.input_transcription and sc.input_transcription.text:
                            self.ev("in", sc.input_transcription.text)
                        if sc.output_transcription and sc.output_transcription.text:
                            self.ev("out", sc.output_transcription.text)
                        if sc.model_turn:
                            for p in sc.model_turn.parts:
                                if p.inline_data and p.inline_data.data:
                                    loud = H.rms(p.inline_data.data) > AUDIO_RMS_MIN
                                    self.ev("audio", loud=loud)
                                    self.on_audio(self.name, p.inline_data.data, loud)
                        if sc.turn_complete:
                            self.ev("turn_complete")
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.ev("closed", repr(e)[:120])
            self.session = None
            if not self.stop:
                await asyncio.sleep(0.3)
                if not self.handle:
                    self.ev("closed", "no handle; fresh session")

    async def send(self, **kw):
        s = self.session
        if s is None:
            return  # reconnecting: audio for this tick is lost, as it would be live
        try:
            await s.send_realtime_input(**kw)
        except Exception as e:  # noqa: BLE001
            self.ev("closed", f"send: {e!r}"[:120])


async def run_once(args, segs, rng) -> dict:
    client = make_client(args.backend)
    events: list[H.Event] = []
    t0 = [None]
    clock = lambda: (time.monotonic() - t0[0]) if t0[0] else -1.0
    speaker_q: list[tuple[str, bytes]] = []  # (origin session, 16k pcm) to play

    def on_audio(origin, pcm24, loud):
        if args.feedback and loud:
            pcm16 = H.resample_24k_to_16k(pcm24)
            for i in range(0, len(pcm16), H.CHUNK_BYTES):
                speaker_q.append((origin, pcm16[i:i + H.CHUNK_BYTES]))

    gated = args.input == "gated"
    sides = {n: Side(n, client, gated, events, clock, on_audio) for n in ("doctor", "patient")}
    for s in sides.values():
        s.task = asyncio.create_task(s.run())
    await asyncio.wait_for(asyncio.gather(*(s.ready.wait() for s in sides.values())), 30)

    total = segs[-1].end_chunk + round(args.drain * 10)
    last_speaker = None
    released: set[str] = set()
    t0[0] = time.monotonic()
    for tick in range(total):
        seg = H.segment_at(segs, tick)
        if seg:
            last_speaker = seg.speaker
        floor = H.noise_chunk(rng, args.floor)
        mic = H.clip_chunk(seg, tick) if seg else floor
        if seg:
            mic = H.mix(mic, floor)
        playing_origin = None
        if speaker_q:
            playing_origin, pb = speaker_q.pop(0)
            mic = H.mix(mic, pb, 0.1)  # -20 dB speaker->mic

        sends = []
        if gated:
            for name, side in sides.items():
                sends.extend(gated_actions(side, name, segs, tick, mic))
        else:
            live = H.route(args.sessions, last_speaker)
            if args.stream_end is not None:
                # The speaker taps "done" stream_end s after finishing: the mic is
                # released (audio_stream_end flushes the model), nothing until re-routed.
                for s in segs:
                    if tick == s.end_chunk + round(args.stream_end * 10):
                        released.add(s.speaker)
                        sends.append(sides[s.speaker].send(audio_stream_end=True))
                if seg:
                    released.discard(seg.speaker)
                live = live - released
            for name, side in sides.items():
                if args.stream_end is not None and name in released:
                    continue
                if args.suppress and playing_origin and name != playing_origin:
                    data = floor
                elif name in live:
                    data = mic
                elif args.idle == "nothing":
                    continue
                else:
                    data = floor if args.idle == "noise" else b"\0" * H.CHUNK_BYTES
                sends.append(side.send(audio=types.Blob(data=data, mime_type=MIME)))
        if sends:
            await asyncio.gather(*sends)
        # absolute pacing: no drift over a long run
        delay = t0[0] + (tick + 1) * 0.1 - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)

    for s in sides.values():
        s.stop = True
        s.task.cancel()
    await asyncio.gather(*(s.task for s in sides.values()), return_exceptions=True)
    return {"events": [e.__dict__ for e in events], **H.score(events, segs, args.tail)}


def gated_actions(side, name, segs, tick, mic):
    """The app's gated turn, per side: activity_start at clip start; clip chunks; 1.2 s
    of mic tail; nothing until 3 s of quiet; then 800 ms of zeros; activity_end."""
    acts = []
    blob = lambda d: side.send(audio=types.Blob(data=d, mime_type=MIME))
    for s in segs:
        if s.speaker != name:
            continue
        rel = tick - s.start_chunk
        end = s.n_chunks
        if rel == 0:
            acts.append(side.send(activity_start=types.ActivityStart()))
        if 0 <= rel < end + 12:              # clip + 1.2 s tail of real mic
            acts.append(blob(mic))
        elif end + 30 <= rel < end + 38:     # stopTalk at 3 s quiet -> 800 ms zeros
            acts.append(blob(b"\0" * H.CHUNK_BYTES))
        if rel == end + 38:
            acts.append(side.send(activity_end=types.ActivityEnd()))
    return acts


def main():
    load_dotenv()
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--input", choices=["gated", "continuous"], default="continuous")
    p.add_argument("--sessions", choices=["routed", "both"], default="routed")
    p.add_argument("--idle", choices=["noise", "zero", "nothing"], default="noise")
    p.add_argument("--floor", type=float, default=30.0)
    p.add_argument("--feedback", action="store_true")
    p.add_argument("--suppress", action="store_true")
    p.add_argument("--stream-end", type=float, default=None, metavar="S",
                   help="send audio_stream_end S seconds after each clip, then nothing until "
                        "that speaker talks again (option A 'done' tap)")
    p.add_argument("--clips", default="conv", help=f"preset ({', '.join(PRESETS)}) or comma list")
    p.add_argument("--gap", type=float, default=5.0)
    p.add_argument("--drain", type=float, default=8.0)
    p.add_argument("--tail", type=float, default=6.0, help="attribution window after a clip")
    p.add_argument("--runs", type=int, default=1)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--backend", choices=["apikey", "enterprise"], default="apikey")
    p.add_argument("--out", help="write JSON results here")
    p.add_argument("--label", default="")
    p.add_argument("--instruction", choices=["off", "on"], default="off",
                   help="send the app's per-direction system instruction (prompt.py)")
    args = p.parse_args()
    global INSTRUCTION
    INSTRUCTION = args.instruction == "on"
    if args.input == "gated" and args.sessions != "routed":
        p.error("gated input is the app's routed mode only")

    specs = H.load_clip_specs()
    names = PRESETS.get(args.clips, args.clips.split(","))
    clips = [(n, specs[n][1], specs[n][0], read_wav(H.CLIPS_DIR / f"{n}.wav")) for n in names]
    # The gated app needs a gap >= its 3.8 s turn close-out before the next speaker.
    gap = max(args.gap, 4.0) if args.input == "gated" else args.gap
    segs = H.build_schedule(clips, gap)
    label = args.label or (f"{args.input}/{args.sessions}/idle={args.idle}/floor={args.floor:g}"
                           f"{'/fb' if args.feedback else ''}{'/sup' if args.suppress else ''}"
                           f"{f'/se={args.stream_end:g}' if args.stream_end is not None else ''}/gap={gap:g}"
                           f"{'/si' if args.instruction == 'on' else ''}")
    results = []
    for r in range(args.runs):
        rng = random.Random(args.seed + r)
        res = asyncio.run(run_once(args, segs, rng))
        res["label"], res["run"] = label, r
        res["schedule"] = {"clips": names, "gap": gap}
        results.append(res)
        a = res["agg"]
        print(f"{label} run{r}: recall={a['mean_input_recall']} tails_dropped={a['tails_dropped']} "
              f"empty={a['empty_outputs']} late={a['late_out_chars']} spurious={a['spurious_out_chars']} "
              f"first_audio={a['median_first_audio_s']} last_out={a['median_last_out_after_end_s']} "
              f"closes={a['closes']} tc={a['turn_completes']}", flush=True)
        for row in res["rows"]:
            print(f"   {row['clip']:8} rec={row['input_recall']:.2f} tail={'Y' if row['tail_heard'] else 'N'} "
                  f"fa={row['first_audio_s']} lo={row['last_out_after_end_s']} late={row['late_out_chars']} "
                  f"| heard={row['heard'][:50]!r} said={row['said'][:60]!r}", flush=True)
        if a["spurious_text"]:
            print(f"   spurious: {a['spurious_text']!r}", flush=True)
    if args.out:
        Path(args.out).write_text(json.dumps(results, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
