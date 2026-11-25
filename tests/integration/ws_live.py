# /// script
# requires-python = ">=3.11"
# dependencies = ["websockets"]
# ///
"""Live protocol test: drives a RUNNING server's /ws exactly like the browser does
(continuous 100 ms mic chunks, TALK = route, release = route None) through the
real Gemini API, and checks each clip's translation lands on the right side with
its final clause intact.

  PORT=8010 uv run server.py &
  uv run tests/integration/ws_live.py --url ws://localhost:8010/ws [--clips conv] [--runs 1]

Exit code 0 only if every clip passes (non-empty, right side, OUT_TAIL keyword).
"""
import argparse
import asyncio
import base64
import json
import random
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "experiments"))
import harness_lib as H  # noqa: E402
import websockets  # noqa: E402

PRESETS = {
    "conv": ["turn-d1", "turn-p1", "turn-d2", "turn-p2", "turn-d3",
             "turn-p3", "turn-d4", "turn-p4", "turn-d5", "turn-p5"],
    "hard": ["s11-d1", "c-p1", "s07-d1", "s08-p1", "b13-d1", "turn-p4"],
    "smoke": ["turn-d1", "turn-p1"],
}


def read(n):
    with wave.open(str(H.CLIPS_DIR / f"{n}.wav")) as w:
        return w.readframes(w.getnframes())


async def run(url, names, gap_s, release_s, floor):
    specs = H.load_clip_specs()
    rng = random.Random(0)
    lines = []        # {side, clip, source, translation}
    current = {}      # side -> index into lines of its open line
    errors = []
    async with websockets.connect(url, max_size=None) as ws:
        await ws.send(json.dumps({"type": "config", "doctor": "en", "patient": "zh"}))
        assert json.loads(await ws.recv())["type"] == "ready"

        async def reader():
            async for raw in ws:
                m = json.loads(raw)
                if m["type"] == "error":
                    errors.append(m.get("raw"))
                if m["type"] in ("source", "translation") and m.get("side") in current:
                    lines[current[m["side"]]][m["type"]] += m["text"]

        rt = asyncio.create_task(reader())
        t0 = time.monotonic()
        tick = 0

        async def send_chunk(pcm):
            nonlocal tick
            await ws.send(json.dumps({"type": "audio", "b64": base64.b64encode(pcm).decode()}))
            tick += 1
            d = t0 + tick * 0.1 - time.monotonic()
            if d > 0:
                await asyncio.sleep(d)

        floor_chunk = lambda: H.noise_chunk(rng, floor)
        for _ in range(10):
            await send_chunk(floor_chunk())
        for n in names:
            side = H.speaker_of(n)
            lines.append({"side": side, "clip": n, "source": "", "translation": ""})
            current[side] = len(lines) - 1
            await ws.send(json.dumps({"type": "route", "side": side}))   # TALK tap
            pcm = read(n)
            for i in range(0, len(pcm), H.CHUNK_BYTES):
                c = pcm[i:i + H.CHUNK_BYTES]
                await send_chunk(H.mix(c + b"\0" * (H.CHUNK_BYTES - len(c)), floor_chunk()))
            for _ in range(round(release_s * 10)):                       # speaker pauses
                await send_chunk(floor_chunk())
            await ws.send(json.dumps({"type": "route", "side": None}))   # done tap
            for _ in range(round(gap_s * 10)):                           # handover
                await send_chunk(floor_chunk())
        for _ in range(60):
            await send_chunk(floor_chunk())
        rt.cancel()

    ok = True
    for ln in lines:
        lang = specs[ln["clip"]][1]
        tail = H.out_tail_ok(ln["clip"], ln["translation"])
        good = bool(ln["translation"]) and tail is not False
        ok &= good
        print(f"{'PASS' if good else 'FAIL'} {ln['clip']:8} [{ln['side']}] heard={ln['source'][:50]!r}"
              f" said={ln['translation'][:70]!r}")
        _ = lang
    if errors:
        ok = False
        print("server errors:", errors)
    return ok


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="ws://localhost:8010/ws")
    p.add_argument("--clips", default="conv")
    p.add_argument("--gap", type=float, default=3.0, help="seconds between release and next TALK")
    p.add_argument("--release", type=float, default=1.5, help="seconds after speech before the done tap")
    p.add_argument("--floor", type=float, default=30.0)
    p.add_argument("--runs", type=int, default=1)
    a = p.parse_args()
    names = PRESETS.get(a.clips, a.clips.split(","))
    results = [asyncio.run(run(a.url, names, a.gap, a.release, a.floor)) for _ in range(a.runs)]
    print(f"\n{sum(results)}/{len(results)} runs fully passed")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
