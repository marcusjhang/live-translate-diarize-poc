# /// script
# requires-python = ">=3.11"
# dependencies = ["google-genai"]
# ///
"""Minimal repro: Google's live-translate starter, fed a recorded clip.

Purpose: tell apart "the model/backend misbehaves" from "our app's config
(manual VAD, resumption, compression, warm-up) misbehaves".

Defaults are EXACTLY the starter: auto VAD, no resumption, no compression,
echo_target_language=True, one session. Audio follows the spec: 16 kHz mono
LE PCM16 in, 100 ms chunks, real-time paced; output saved as 24 kHz PCM16.

  uv run experiments/starter_repro.py tests/e2e/clips/b14-p1.wav --target en
  uv run experiments/starter_repro.py CLIP --backend enterprise   # Vertex / Agent Platform (needs ADC)
  uv run experiments/starter_repro.py CLIP --manual-vad          # A/B: our app's turn mode

--backend apikey      Developer API, GEMINI_API_KEY from .env, api_version v1beta (what server.py uses)
--backend enterprise  genai.Client(enterprise=True, project=..., location=...) as in the starter.
                      Needs `gcloud auth application-default login` and GOOGLE_CLOUD_PROJECT
                      (+ optional GOOGLE_CLOUD_LOCATION, default us-central1).
"""
import argparse
import asyncio
import os
import time
import wave
from pathlib import Path

from google import genai
from google.genai import types

MODEL = "gemini-3.5-live-translate-preview"
IN_RATE, OUT_RATE = 16000, 24000
CHUNK_BYTES = IN_RATE * 2 // 10  # 100 ms of 16 kHz mono PCM16 = 3200 bytes


def load_dotenv():
    env = Path(__file__).resolve().parent.parent / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


def read_clip(path: str) -> bytes:
    with wave.open(path) as w:
        rate, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        if (rate, ch, width) != (IN_RATE, 1, 2):
            raise SystemExit(f"{path}: need 16 kHz mono 16-bit, got {rate} Hz / {ch} ch / {width * 8}-bit")
        return w.readframes(w.getnframes())  # WAV PCM is already little-endian


def make_client(backend: str):
    if backend == "enterprise":
        return genai.Client(
            enterprise=True,
            project=os.environ["GOOGLE_CLOUD_PROJECT"],
            location=os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1"),
        )
    return genai.Client(api_key=os.environ["GEMINI_API_KEY"], http_options={"api_version": "v1beta"})


def make_config(target: str, echo: bool, manual_vad: bool):
    cfg = dict(
        response_modalities=["AUDIO"],
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        translation_config=types.TranslationConfig(target_language_code=target, echo_target_language=echo),
    )
    if manual_vad:
        cfg["realtime_input_config"] = types.RealtimeInputConfig(
            automatic_activity_detection=types.AutomaticActivityDetection(disabled=True))
    return types.LiveConnectConfig(**cfg)


async def run(args):
    pcm = read_clip(args.clip)
    client = make_client(args.backend)
    config = make_config(args.target, not args.no_echo, args.manual_vad)
    t0 = time.monotonic()
    ts = lambda: f"{time.monotonic() - t0:6.2f}s"
    out_audio = bytearray()
    first_audio = None

    print(f"[{ts()}] backend={args.backend} vad={'manual' if args.manual_vad else 'auto'} "
          f"target={args.target} clip={args.clip} ({len(pcm) / (IN_RATE * 2):.2f}s)")
    async with client.aio.live.connect(model=MODEL, config=config) as session:
        print(f"[{ts()}] connected")

        async def sender():
            silence = b"\x00\x00" * (CHUNK_BYTES // 2)
            if args.manual_vad:
                await session.send_realtime_input(activity_start=types.ActivityStart())
            for _ in range(int(args.lead * 10)):  # audio before the speech starts
                await session.send_realtime_input(
                    audio=types.Blob(data=silence, mime_type="audio/pcm;rate=16000"))
                await asyncio.sleep(0.1)
            for i in range(0, len(pcm), CHUNK_BYTES):
                await session.send_realtime_input(
                    audio=types.Blob(data=pcm[i:i + CHUNK_BYTES], mime_type="audio/pcm;rate=16000"))
                await asyncio.sleep(0.1)  # real-time pacing, like a mic
            print(f"[{ts()}] clip sent; streaming {args.tail}s of silence")
            # A mic keeps streaming after speech stops; auto VAD needs that to close the turn.
            for _ in range(int(args.tail * 10)):
                if args.manual_vad and _ == 10:
                    await session.send_realtime_input(activity_end=types.ActivityEnd())
                await session.send_realtime_input(
                    audio=types.Blob(data=silence, mime_type="audio/pcm;rate=16000"))
                await asyncio.sleep(0.1)
            print(f"[{ts()}] done sending")

        async def receiver():
            nonlocal first_audio
            async for r in session.receive():
                sc = r.server_content
                if r.go_away:
                    print(f"[{ts()}] GO_AWAY")
                if not sc:
                    continue
                if sc.input_transcription and sc.input_transcription.text:
                    print(f"[{ts()}] IN : {sc.input_transcription.text!r}")
                if sc.output_transcription and sc.output_transcription.text:
                    print(f"[{ts()}] OUT: {sc.output_transcription.text!r}")
                if sc.model_turn:
                    for part in sc.model_turn.parts:
                        if part.inline_data and part.inline_data.data:
                            if first_audio is None:
                                first_audio = time.monotonic() - t0
                                print(f"[{ts()}] first audio ({part.inline_data.mime_type})")
                            out_audio.extend(part.inline_data.data)
                if sc.turn_complete:
                    print(f"[{ts()}] turn_complete")

        recv_t = asyncio.create_task(receiver())
        try:
            await sender()
            await asyncio.sleep(args.drain)  # let late output arrive
        finally:
            recv_t.cancel()
            try:
                await recv_t
            except (asyncio.CancelledError, Exception) as e:  # noqa: BLE001
                if not isinstance(e, asyncio.CancelledError):
                    print(f"[{ts()}] receive ended: {e!r}")

    out = Path(args.out or f"/tmp/repro-{Path(args.clip).stem}-{args.backend}-"
                             f"{'manual' if args.manual_vad else 'auto'}.wav")
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(OUT_RATE); w.writeframes(bytes(out_audio))
    print(f"[{ts()}] output audio {len(out_audio) / (OUT_RATE * 2):.2f}s -> {out}")


def main():
    load_dotenv()
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("clip", help="16 kHz mono 16-bit WAV")
    p.add_argument("--target", default="en", help="target BCP-47 code (zh-Hans for Mandarin)")
    p.add_argument("--backend", choices=["apikey", "enterprise"], default="apikey")
    p.add_argument("--manual-vad", action="store_true", help="disable auto VAD (our app's mode)")
    p.add_argument("--no-echo", action="store_true", help="echo_target_language=False (our app's setting)")
    p.add_argument("--lead", type=float, default=0.0, help="seconds of silence before the clip")
    p.add_argument("--tail", type=float, default=3.0, help="seconds of silence streamed after the clip")
    p.add_argument("--drain", type=float, default=5.0, help="seconds to keep receiving after sending")
    p.add_argument("--out", help="output WAV path (24 kHz)")
    asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    main()
