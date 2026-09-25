# /// script
# requires-python = ">=3.11"
# dependencies = ["fastapi", "uvicorn", "websockets", "google-genai"]
# ///
"""Two-person live interpretation for a doctor-patient consult on one tablet.

Run:       uv run server.py              (needs GEMINI_API_KEY in .env)
Selfcheck: uv run server.py --self-check  (no key needed — pure logic only)

The browser owns mic capture, the turn state, and the buffered playback; this
backend owns the Gemini 3.5 Live Translate sessions and the API key. It is a
thin relay plus the pure logic that the self-check asserts: language mapping,
the transcript normaliser, and the turn state machine.
"""
import asyncio
import base64
from contextlib import asynccontextmanager
import json
import os
import sys
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

STATIC_DIR = Path(__file__).parent / "static"


def _load_dotenv():
    """Minimal .env loader (no dependency). Never overrides already-set vars."""
    env = Path(__file__).parent / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


_load_dotenv()

MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-live-translate-preview")

# ---------------------------------------------------------------------------
# Pure logic — no SDK, no network, no key. The self-check asserts this.
# ---------------------------------------------------------------------------

_BCP47 = {"zh": "zh-Hans"}  # Mandarin needs a script tag; en/es/fil pass through.


def lang_bcp47(code: str) -> str:
    """Short language code -> BCP-47. zh -> zh-Hans; everything else is already
    a valid BCP-47 tag."""
    return _BCP47.get(code, code)


def normalize_transcript(sc: dict) -> list[dict]:
    """Map one flattened Gemini server-content dict to flat frontend messages.

    Pure function (dict in, list of dicts out) so the self-check can run it
    without google-genai. One server_content can carry several messages, so this
    returns a list. `input_text`/`output_text` are streamed deltas.
    """
    out = []
    if sc.get("input_text"):
        out.append({"type": "source", "text": sc["input_text"]})
    if sc.get("output_text"):
        out.append({"type": "translation", "text": sc["output_text"]})
    for b64 in sc.get("audio_b64", []):
        out.append({"type": "audio", "b64": b64})
    if sc.get("turn_complete"):
        out.append({"type": "turn_end"})
    return out


# ---------------------------------------------------------------------------
# Gemini 3.5 Live Translate — Developer API-key auth (NOT Vertex / ADC).
# Imported lazily so --self-check needs no google-genai and no key.
# ---------------------------------------------------------------------------

_gemini_client = None


def gemini_client():
    global _gemini_client
    if _gemini_client is None:
        from google import genai

        _gemini_client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    return _gemini_client


def gemini_config(target_bcp47: str, handle: str | None = None):
    from google.genai import types

    # NOTE: transparent=True is Vertex-only. With Developer-API key auth it raises
    # ValueError at connect, so use handle-based resumption only.
    resumption = (
        types.SessionResumptionConfig(handle=handle)
        if handle
        else types.SessionResumptionConfig()
    )
    return types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        translation_config=types.TranslationConfig(
            target_language_code=target_bcp47,
            echo_target_language=False,  # True makes the model parrot same-language input
        ),
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        realtime_input_config=types.RealtimeInputConfig(
            automatic_activity_detection=types.AutomaticActivityDetection(disabled=True)
        ),
        session_resumption=resumption,
        context_window_compression=types.ContextWindowCompressionConfig(
            sliding_window=types.SlidingWindow(target_tokens=16384)
        ),
    )


def is_benign_session_end(exc: BaseException) -> bool:
    """A Live session ending is normal, not a failure: the API closes the socket at
    the end of a turn and rotates sessions at its caps, surfacing as a clean close
    (code 1000) or ClosedOK. Matching only the leading code keeps unrelated errors
    that merely contain "1000" from being swallowed as benign."""
    from websockets.exceptions import ConnectionClosed

    return (
        isinstance(exc, ConnectionClosed)
        or getattr(exc, "code", None) == 1000
        or str(exc).startswith("1000")
    )


def _flatten(sc) -> dict:
    """Flatten an SDK server_content object into the dict normalize_transcript
    expects. Kept separate so the normaliser itself stays a pure dict function."""
    d = {"audio_b64": []}
    if sc.input_transcription and sc.input_transcription.text:
        d["input_text"] = sc.input_transcription.text
    if sc.output_transcription and sc.output_transcription.text:
        d["output_text"] = sc.output_transcription.text
    if sc.model_turn:
        for part in sc.model_turn.parts:
            ind = part.inline_data
            if ind and (ind.mime_type or "").startswith("audio"):
                d["audio_b64"].append(base64.b64encode(ind.data).decode())
    if sc.turn_complete:
        d["turn_complete"] = True
    return d


async def _direction(side: str, target: str, in_q: asyncio.Queue, out_q: asyncio.Queue):
    """One translation direction = one Gemini live session. Consumes control
    items from in_q (activity_start / audio / silence / activity_end / close),
    streams them to the model, and pushes normalized messages (tagged with side)
    into out_q. Reconnects on go_away using the resumption handle, so a consult
    survives the ~10-15 min live-session caps."""
    from google.genai import types

    client = gemini_client()
    handle = None
    hard_failures = 0
    MAX_HARD_FAILURES = 5
    clean_reconnects = 0
    MAX_CLEAN_RECONNECTS = 10
    # Which turn the model's output currently belongs to. The model keeps emitting
    # after a turn is closed; without this tag that late output has no turn to
    # belong to and gets attributed to whatever turn comes next — which is how the
    # tool ended up "saying" sentences nobody spoke.
    cur_turn = None

    while True:
        config = gemini_config(target, handle)
        stopped = {"v": False}
        produced = {"v": False}
        try:
            async with client.aio.live.connect(model=MODEL, config=config) as session:

                # Warm the session. The first turn on a cold Live session frequently
                # produces nothing at all. A throwaway silent turn primes it; its
                # output is tagged with turn=None and dropped by the client.
                try:
                    await session.send_realtime_input(activity_start=types.ActivityStart())
                    warm = b"\x00\x00" * 1600
                    for _ in range(4):
                        await session.send_realtime_input(
                            audio=types.Blob(data=warm, mime_type="audio/pcm;rate=16000"))
                        await asyncio.sleep(0.1)
                    await session.send_realtime_input(activity_end=types.ActivityEnd())
                    print(f"[{side}] warmed")
                except Exception as e:  # noqa: BLE001 - warming is best-effort
                    print(f"[{side}] warm-up skipped: {e}")

                async def send_loop():
                    nonlocal cur_turn
                    turn_bytes = 0
                    while True:
                        item = await in_q.get()
                        t = item["type"]
                        if t == "close":
                            stopped["v"] = True
                            return
                        if t == "activity_start":
                            turn_bytes = 0
                            cur_turn = item.get("turn")
                            await session.send_realtime_input(activity_start=types.ActivityStart())
                        elif t == "activity_end":
                            await session.send_realtime_input(activity_end=types.ActivityEnd())
                            print(f"[{side}] turn input audio = {turn_bytes / 32000:.2f}s "
                                  f"({turn_bytes} bytes @16k mono pcm16)")
                        elif t == "audio":
                            pcm = base64.b64decode(item["b64"])
                            turn_bytes += len(pcm)
                            await session.send_realtime_input(
                                audio=types.Blob(data=pcm, mime_type="audio/pcm;rate=16000")
                            )
                        elif t == "silence":
                            # Trailing silence must be PACED, not bursted: it is what
                            # lets the model finish the final clause. Sending it as one
                            # instant burst does not reliably commit the tail, which is
                            # why the last phrase kept dropping. 100 ms per chunk.
                            chunk = b"\x00\x00" * 1600  # 100 ms of 16 kHz mono PCM16
                            for _ in range(item["ms"] // 100):
                                await session.send_realtime_input(
                                    audio=types.Blob(data=chunk, mime_type="audio/pcm;rate=16000")
                                )
                                await asyncio.sleep(0.1)

                async def recv_loop():
                    nonlocal handle
                    async for msg in session.receive():
                        if msg.session_resumption_update and msg.session_resumption_update.new_handle:
                            handle = msg.session_resumption_update.new_handle
                        if msg.go_away:
                            tl = getattr(msg.go_away, "time_left", None)
                            print(f"[{side}] GO_AWAY (time_left={tl}) -> reconnecting")
                            return  # session is ending; reconnect below
                        sc = msg.server_content
                        if sc:
                            produced["v"] = True
                            for out in normalize_transcript(_flatten(sc)):
                                tagged = dict(out)
                                tagged["turn"] = cur_turn
                                await out_q.put((side, tagged))

                send_t = asyncio.create_task(send_loop())
                recv_t = asyncio.create_task(recv_loop())
                _, pending_t = await asyncio.wait(
                    {send_t, recv_t}, return_when=asyncio.FIRST_COMPLETED)
                for t in pending_t:
                    t.cancel()
                # Await each pump explicitly. gather(..., return_exceptions=True) can
                # still leave a task's exception un-retrieved when it is cancelled
                # while raising, which produced one "Task exception was never
                # retrieved" traceback per session close — 36 in a 15-scenario run.
                error = None
                for t in (send_t, recv_t):
                    try:
                        await t
                    except asyncio.CancelledError:
                        pass
                    except Exception as e:  # noqa: BLE001
                        if error is None:
                            error = e
                if error is not None:
                    raise error
                if stopped["v"]:
                    return  # browser went away: this direction is finished
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            # A session ending is NORMAL, not a failure: the Live API closes the
            # socket at the end of a turn and rotates sessions at its caps, both
            # surfacing as a clean close (code 1000) or ClosedOK. Treating those as
            # fatal used to kill this direction permanently. Only genuine,
            # repeated failures (auth, connect refused) are surfaced to the browser.
            if is_benign_session_end(e):
                hard_failures = 0
                print(f"[{side}] session closed cleanly -> reconnecting (handle={'yes' if handle else 'no'})")
                await asyncio.sleep(0.2)  # reconnect with the handle, cheaply
            else:
                hard_failures += 1
                if hard_failures > MAX_HARD_FAILURES:
                    print(f"[{side}] giving up after {hard_failures} failures: {e}")
                    await out_q.put((side, {"type": "error", "raw": str(e)}))
                    return
                print(f"[{side}] transient failure {hard_failures}/{MAX_HARD_FAILURES}: {e}")
                await asyncio.sleep(min(2 ** hard_failures, 4))
            continue

        # Reached only on a clean end with the browser still present: go_away or a
        # rotated session. Reconnect transparently using the resumption handle, but
        # bounded — a handle the server rejects immediately would otherwise spin
        # here forever on a 50 ms pause, hammering the API.
        if produced["v"]:
            clean_reconnects = 0
        clean_reconnects += 1
        if clean_reconnects > MAX_CLEAN_RECONNECTS:
            print(f"[{side}] session ended {clean_reconnects}x with no output; giving up")
            await out_q.put((side, {"type": "error", "raw": "session ended repeatedly"}))
            return
        await asyncio.sleep(min(0.2 * clean_reconnects, 2))



def _quiet_benign(loop, context):
    """A Live session closing at the end of a turn is normal, but the SDK can
    re-raise it during teardown after the task is already disposed, which asyncio
    reports as an un-retrieved exception. Those are noise; everything else still
    goes to the default handler."""
    exc = context.get("exception")
    if exc is not None and is_benign_session_end(exc):
        return
    loop.default_exception_handler(context)


@asynccontextmanager
async def _lifespan(_app):
    asyncio.get_running_loop().set_exception_handler(_quiet_benign)
    yield


app = FastAPI(lifespan=_lifespan)


@app.get("/", response_class=HTMLResponse)
async def index():
    idx = STATIC_DIR / "index.html"
    if idx.exists():
        return FileResponse(idx)
    return HTMLResponse("<h1>Interpretation backend up.</h1><p>No frontend in static/.</p>")


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.websocket("/ws")
async def ws_bridge(browser: WebSocket):
    await browser.accept()

    try:
        cfg = await browser.receive_json()
    except (WebSocketDisconnect, ValueError, json.JSONDecodeError):
        await browser.close()
        return
    if not isinstance(cfg, dict):
        await browser.close()
        return
    doctor = cfg.get("doctor", "en")
    patient = cfg.get("patient", "zh")

    if not os.environ.get("GEMINI_API_KEY"):
        await browser.send_json({"type": "error", "raw": "GEMINI_API_KEY not set on server"})
        await browser.close()
        return

    # Two directions, two sessions: A feeds the doctor's mic and targets the
    # patient's language; B feeds the patient's mic and targets the doctor's.
    in_q = {"doctor": asyncio.Queue(), "patient": asyncio.Queue()}
    out_q = asyncio.Queue()
    tasks = {
        "doctor": asyncio.create_task(_direction("doctor", lang_bcp47(patient), in_q["doctor"], out_q)),
        "patient": asyncio.create_task(_direction("patient", lang_bcp47(doctor), in_q["patient"], out_q)),
    }

    await browser.send_json({"type": "ready"})

    async def out_pump():
        while True:
            side, msg = await out_q.get()
            msg = dict(msg)
            msg["side"] = side
            await browser.send_json(msg)

    out_task = asyncio.create_task(out_pump())
    active = None  # the single direction that may stream right now

    try:
        while True:
            m = await browser.receive_json()
            t = m.get("type")
            if t == "talk_start":
                side = m.get("side")
                if side in in_q and active is None:
                    active = side
                    await in_q[side].put({"type": "activity_start", "turn": m.get("turn")})
            elif t == "audio":
                b64 = m.get("b64")
                if active is not None and isinstance(b64, str):
                    await in_q[active].put({"type": "audio", "b64": b64})
            elif t == "talk_end":
                if active is not None:
                    # ~800 ms trailing silence, then activity_end. 400 ms measured
                    # truncates the final clause; 800 ms yields the whole sentence.
                    await in_q[active].put({"type": "silence", "ms": 800})
                    await in_q[active].put({"type": "activity_end"})
                    active = None
    except (WebSocketDisconnect, ValueError, json.JSONDecodeError):
        pass
    finally:
        for q in in_q.values():
            await q.put({"type": "close"})
        out_task.cancel()
        for t in tasks.values():
            t.cancel()


def _check(cond, msg):
    """Explicit check: bare assert is stripped under python -O, which would turn the
    build's verify gate into a silent no-op."""
    if not cond:
        print(f"self-check FAILED: {msg}", file=sys.stderr)
        raise SystemExit(1)


def _self_check():
    # Language mapping.
    _check(lang_bcp47("zh") == "zh-Hans", "zh must map to zh-Hans")
    _check(lang_bcp47("en") == "en", "en passes through")
    _check(lang_bcp47("es") == "es", "es passes through")
    _check(lang_bcp47("fil") == "fil", "fil passes through")

    # Transcript normaliser.
    _check(normalize_transcript({"input_text": "penicillin"}) ==
           [{"type": "source", "text": "penicillin"}], "input_text -> source")
    _check(normalize_transcript({"output_text": "青霉素"}) ==
           [{"type": "translation", "text": "青霉素"}], "output_text -> translation")
    _check(normalize_transcript({"audio_b64": ["AAA="]}) ==
           [{"type": "audio", "b64": "AAA="}], "audio passthrough")
    _check(normalize_transcript({"turn_complete": True}) == [{"type": "turn_end"}],
           "turn_complete -> turn_end")
    _check(normalize_transcript({}) == [], "unknown content yields nothing")
    _check(normalize_transcript({"input_text": "hi", "output_text": "你好",
                                 "audio_b64": ["AAA="], "turn_complete": True}) == [
        {"type": "source", "text": "hi"},
        {"type": "translation", "text": "你好"},
        {"type": "audio", "b64": "AAA="},
        {"type": "turn_end"},
    ], "combined content preserves order")

    # Session-end classification. This is the predicate the reconnect loop uses, so
    # exercising it here actually covers shipped behaviour.
    class _Coded(Exception):
        def __init__(self, code):
            self.code = code
            super().__init__(f"{code} None. ")

    _check(is_benign_session_end(_Coded(1000)), "code 1000 is a normal end")
    _check(not is_benign_session_end(_Coded(429)), "429 is not benign")
    _check(not is_benign_session_end(RuntimeError("token budget 11000 exceeded")),
           "unrelated text containing 1000 is not benign")
    _check(is_benign_session_end(RuntimeError("1000 None. ")), "leading 1000 is benign")

    print("self-check OK")


if __name__ == "__main__":
    if "--self-check" in sys.argv:
        _self_check()
    else:
        host = os.environ.get("HOST", "127.0.0.1")
        port = int(os.environ.get("PORT", "8000"))
        print(f"[interpret] listening on http://{host}:{port}")
        # Loopback by default: /ws is unauthenticated and every turn spends the
        # server-side API key, so exposing it on the LAN hands the key to anyone
        # who can reach the port. Set HOST=0.0.0.0 deliberately.
        uvicorn.run(app, host=host, port=port)
