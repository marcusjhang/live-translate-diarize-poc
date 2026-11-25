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
the transcript normaliser, mic routing and the bounded input queue.

Input model (docs/starter-alignment-plan.md): exactly Google's starter, once per
direction. The browser streams the mic as one continuous 16 kHz PCM16 stream in
100 ms chunks; automatic activity detection decides where speech starts and ends.
The TALK buttons only ROUTE that stream to one direction's session. Releasing
the route sends `audio_stream_end`, which makes the model flush the clause it is
still holding (it reorders, so it often waits for more audio).
"""
import asyncio
import argparse
import base64
import collections
from contextlib import asynccontextmanager
import json
import os
import sys
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from streaming_mode import STREAMING_MODES, default_streaming_mode, parse_streaming_mode
from prompt import LANG_NAMES, system_instruction

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
DEFAULT_STREAMING_MODE = default_streaming_mode()

# ---------------------------------------------------------------------------
# Pure logic — no SDK, no network, no key. The self-check asserts this.
# ---------------------------------------------------------------------------

_BCP47 = {"zh": "zh-Hans"}  # Mandarin needs a script tag; every other UI code
# (ta, id, vi, zh-Hant, pt-BR, ...) is already a BCP-47 tag and passes through.


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


SIDES = ("doctor", "patient")


def reroute(current: str | None, new: str | None, flush: bool = True):
    """Move the mic route from `current` to `new` (a side or None = released).
    Returns (route, actions): actions are (side, item) pairs to enqueue. The side
    losing the mic gets `stream_end` so the model flushes its pending clause."""
    if new not in SIDES:
        new = None
    actions = []
    if flush and current is not None and current != new:
        actions.append((current, {"type": "stream_end"}))
    return new, actions


INPUT_BACKLOG = 20  # chunks (2 s) a direction may buffer while it reconnects
PREROLL_CHUNKS = 3  # 300 ms of mic audio sent ahead of a newly routed stream
PREROLL_MAX_AGE = 0.45  # s; older chunks are not "just before the tap" any more

# The browser streams the mic continuously, routed or not. Keeping the last few
# chunks lets a newly routed session hear the onset of the first word: people
# start talking as (or just before) they tap, and a session fed nothing until
# that instant clipped the first syllable ("咳嗽…" for "我咳嗽…").


def fresh_preroll(buf, now: float, max_age: float = PREROLL_MAX_AGE) -> list[bytes]:
    """The pre-roll chunks still recent enough to be the onset of the speech that
    the tap is about. The browser pauses uploading while translated speech plays,
    so without an age limit the buffer could hold the PREVIOUS speaker's tail
    from seconds ago."""
    return [pcm for t, pcm in buf if now - t <= max_age]


def offer(q: "asyncio.Queue", item: dict, limit: int = INPUT_BACKLOG) -> int:
    """Enqueue without letting a reconnecting direction build a backlog: beyond
    `limit` queued audio chunks the OLDEST audio is dropped (live speech replayed
    late in a burst is worse than a gap). Control items are never dropped.
    Returns the number of chunks dropped."""
    dropped = 0
    if item.get("type") == "audio":
        while sum(1 for x in q._queue if x.get("type") == "audio") >= limit:  # noqa: SLF001
            for x in list(q._queue):  # noqa: SLF001
                if x.get("type") == "audio":
                    q._queue.remove(x)  # noqa: SLF001
                    dropped += 1
                    break
    q.put_nowait(item)
    return dropped


# ---------------------------------------------------------------------------
# Gemini client. Two backends, chosen with GEMINI_BACKEND:
#   apikey     (default) Developer API, GEMINI_API_KEY, api_version v1beta
#   enterprise Google Cloud / Agent Platform, exactly as the starter:
#              genai.Client(enterprise=True, project=..., location=...) with ADC
#              (`gcloud auth application-default login`, GOOGLE_CLOUD_PROJECT,
#              optional GOOGLE_CLOUD_LOCATION, default us-central1).
# Imported lazily so --self-check needs no google-genai and no key.
# ---------------------------------------------------------------------------

BACKENDS = ("apikey", "enterprise")


def backend() -> str:
    b = (os.environ.get("GEMINI_BACKEND") or "apikey").strip().lower()
    if b not in BACKENDS:
        raise ValueError(f"GEMINI_BACKEND must be one of {BACKENDS}, got {b!r}")
    return b


def client_kwargs(b: str, env=os.environ) -> dict:
    """Constructor arguments for genai.Client per backend (pure; self-checked)."""
    if b == "enterprise":
        return {"enterprise": True, "project": env["GOOGLE_CLOUD_PROJECT"],
                "location": env.get("GOOGLE_CLOUD_LOCATION", "us-central1")}
    # Pin the API version: newer google-genai releases default the Developer API to
    # "v1", which does not serve the Live WebSocket (BidiGenerateContent) and
    # rejects every connect with HTTP 404. Live lives on v1beta.
    return {"api_key": env["GEMINI_API_KEY"],
            "http_options": {"api_version": env.get("GEMINI_API_VERSION", "v1beta")}}


def backend_ready(b: str, env=os.environ) -> str | None:
    """None if the backend has what it needs, else the reason (shown to the user)."""
    if b == "enterprise":
        return None if env.get("GOOGLE_CLOUD_PROJECT") else "GOOGLE_CLOUD_PROJECT not set on server"
    return None if env.get("GEMINI_API_KEY") else "GEMINI_API_KEY not set on server"


_gemini_client = None


def gemini_client():
    global _gemini_client
    if _gemini_client is None:
        from google import genai

        _gemini_client = genai.Client(**client_kwargs(backend()))
    return _gemini_client


def gemini_config(target_bcp47: str, handle: str | None = None, instruction: str | None = None):
    """The starter's config, plus session resumption (a consult outlasts one live
    session) and the per-direction system instruction (prompt.py). Automatic
    activity detection stays ON: no realtime_input_config."""
    from google.genai import types

    # transparent=True (server-managed resumption) is Vertex-only; with Developer
    # API key auth it raises ValueError at connect, so that path keeps handles only.
    transparent = {"transparent": True} if backend() == "enterprise" else {}
    resumption = (
        types.SessionResumptionConfig(handle=handle, **transparent)
        if handle
        else types.SessionResumptionConfig(**transparent)
    )
    return types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        system_instruction=instruction,
        translation_config=types.TranslationConfig(
            target_language_code=target_bcp47,
            # False: do not re-speak input that is already in the target language.
            # (It does not fully stop that — see docs/starter-alignment-results.md —
            # which is why the mic is routed to one direction at a time.)
            echo_target_language=False,
        ),
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        session_resumption=resumption,
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


async def _direction(side: str, target: str, in_q: asyncio.Queue, out_q: asyncio.Queue,
                     instruction: str | None = None):
    """One translation direction = one Gemini live session fed like the starter:
    audio chunks as they arrive, plus `audio_stream_end` when the mic is routed
    away. Pushes normalized messages (tagged with side by the caller) into out_q.
    Reconnects on go_away / clean close using the resumption handle, so a consult
    survives the ~10-15 min live-session caps."""
    from google.genai import types

    client = gemini_client()
    handle = None
    hard_failures = 0
    MAX_HARD_FAILURES = 5
    clean_reconnects = 0
    MAX_CLEAN_RECONNECTS = 10

    while True:
        config = gemini_config(target, handle, instruction)
        stopped = {"v": False}
        produced = {"v": False}
        try:
            async with client.aio.live.connect(model=MODEL, config=config) as session:

                async def send_loop():
                    while True:
                        item = await in_q.get()
                        t = item["type"]
                        if t == "close":
                            stopped["v"] = True
                            return
                        try:
                            if t == "audio":
                                await session.send_realtime_input(
                                    audio=types.Blob(data=item["pcm"], mime_type="audio/pcm;rate=16000")
                                )
                            elif t == "stream_end":
                                await session.send_realtime_input(audio_stream_end=True)
                        except asyncio.CancelledError:
                            # go_away mid-send: give the item to the next session
                            # instead of losing a chunk of speech or the flush.
                            in_q._queue.appendleft(item)  # noqa: SLF001
                            raise

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
                                await out_q.put((side, out))

                send_t = asyncio.create_task(send_loop())
                recv_t = asyncio.create_task(recv_loop())
                _, pending_t = await asyncio.wait(
                    {send_t, recv_t}, return_when=asyncio.FIRST_COMPLETED)
                for t in pending_t:
                    t.cancel()
                # Await each pump explicitly. gather(..., return_exceptions=True) can
                # still leave a task's exception un-retrieved when it is cancelled
                # while raising.
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
            # A session ending is NORMAL, not a failure: the API rotates sessions at
            # its caps, surfacing as a clean close (code 1000) or ClosedOK. Only
            # genuine, repeated failures (auth, connect refused) reach the browser.
            if is_benign_session_end(e):
                hard_failures = 0
                print(f"[{side}] session closed cleanly -> reconnecting (handle={'yes' if handle else 'no'})")
                await asyncio.sleep(0.2)
            else:
                hard_failures += 1
                if hard_failures > MAX_HARD_FAILURES:
                    print(f"[{side}] giving up after {hard_failures} failures: {e}")
                    await out_q.put((side, {"type": "error", "raw": str(e)}))
                    return
                print(f"[{side}] transient failure {hard_failures}/{MAX_HARD_FAILURES}: {e}")
                await asyncio.sleep(min(2 ** hard_failures, 4))
            continue

        # Clean end with the browser still present: go_away or a rotated session.
        # Reconnect with the handle, but bounded — a handle the server rejects
        # immediately would otherwise spin here, hammering the API.
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
    try:
        streaming_mode = parse_streaming_mode(cfg.get("streaming_mode", DEFAULT_STREAMING_MODE))
    except ValueError:
        await browser.send_json({"type": "error", "raw": "Invalid streaming mode"})
        await browser.close()
        return

    try:
        why = backend_ready(backend())
    except ValueError as e:
        why = str(e)
    if why:
        await browser.send_json({"type": "error", "raw": why})
        await browser.close()
        return

    # Two directions, two sessions: A feeds the doctor's mic and targets the
    # patient's language; B feeds the patient's mic and targets the doctor's.
    in_q = {"doctor": asyncio.Queue(), "patient": asyncio.Queue()}
    out_q = asyncio.Queue()
    tasks = {
        "doctor": asyncio.create_task(_direction(
            "doctor", lang_bcp47(patient), in_q["doctor"], out_q,
            system_instruction("doctor", doctor, patient))),
        "patient": asyncio.create_task(_direction(
            "patient", lang_bcp47(doctor), in_q["patient"], out_q,
            system_instruction("patient", patient, doctor))),
    }

    await browser.send_json({"type": "ready", "streaming_mode": streaming_mode})

    async def out_pump():
        while True:
            side, msg = await out_q.get()
            msg = dict(msg)
            msg["side"] = side
            await browser.send_json(msg)

    out_task = asyncio.create_task(out_pump())
    route = None  # the direction receiving the mic stream; None = released
    preroll = collections.deque(maxlen=PREROLL_CHUNKS)  # recent unrouted mic audio

    try:
        while True:
            m = await browser.receive_json()
            if not isinstance(m, dict):
                continue
            t = m.get("type")
            if t == "audio":
                b64 = m.get("b64")
                if route is not None and isinstance(b64, str):
                    try:
                        pcm = base64.b64decode(b64, validate=True)
                    except ValueError:
                        continue
                    if pcm and len(pcm) % 2 == 0:
                        dropped = offer(in_q[route], {"type": "audio", "pcm": pcm})
                        if dropped:
                            print(f"[{route}] input backlog: dropped {dropped} stale chunk(s)")
                elif isinstance(b64, str):
                    try:
                        pcm = base64.b64decode(b64, validate=True)
                    except ValueError:
                        continue
                    if pcm and len(pcm) % 2 == 0:
                        preroll.append((time.monotonic(), pcm))
            elif t == "route":
                prev = route
                route, actions = reroute(route, m.get("side"))
                for side, item in actions:
                    offer(in_q[side], item)
                if route is not None and route != prev:
                    for pcm in fresh_preroll(preroll, time.monotonic()):
                        offer(in_q[route], {"type": "audio", "pcm": pcm})
                # Released or switched: whatever was buffered belongs to no one now.
                preroll.clear()
            elif t == "set_streaming_mode":
                try:
                    streaming_mode = parse_streaming_mode(m.get("streaming_mode"))
                except ValueError:
                    await browser.send_json({"type": "error", "raw": "Invalid streaming mode"})
                else:
                    await browser.send_json({
                        "type": "streaming_mode",
                        "streaming_mode": streaming_mode,
                    })
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
    # Delivery-mode configuration.
    _check(parse_streaming_mode(None) == "turn", "turn is the default mode")
    _check(parse_streaming_mode(" TURN ") == "turn", "mode is normalized")
    _check(parse_streaming_mode("realtime") == "realtime", "realtime is valid")
    try:
        parse_streaming_mode("fast")
    except ValueError:
        pass
    else:
        _check(False, "unknown streaming mode must fail")

    # Mic routing: the side losing the mic is flushed, exactly once.
    _check(reroute(None, "doctor") == ("doctor", []), "first route flushes nobody")
    _check(reroute("doctor", "doctor") == ("doctor", []), "same route is a no-op")
    _check(reroute("doctor", "patient") == ("patient", [("doctor", {"type": "stream_end"})]),
           "switching flushes the previous side")
    _check(reroute("patient", None) == (None, [("patient", {"type": "stream_end"})]),
           "release flushes")
    _check(reroute("patient", "nobody") == (None, [("patient", {"type": "stream_end"})]),
           "an unknown side is a release")
    _check(reroute("doctor", "patient", flush=False) == ("patient", []), "flush can be disabled")

    # Pre-roll: only chunks from just before the tap.
    _check(fresh_preroll([(9.0, b"old"), (9.7, b"a"), (9.9, b"b")], now=10.0) == [b"a", b"b"],
           "stale pre-roll is not replayed")
    _check(fresh_preroll([(1.0, b"x")], now=10.0) == [], "pre-roll from long ago is empty")

    # Bounded input queue: stale audio is dropped oldest-first, control never.
    q = asyncio.Queue()
    for i in range(3):
        offer(q, {"type": "audio", "pcm": bytes([i])}, limit=3)
    offer(q, {"type": "stream_end"}, limit=3)
    _check(offer(q, {"type": "audio", "pcm": b"\x03"}, limit=3) == 1, "one stale chunk dropped")
    items = [q.get_nowait() for _ in range(q.qsize())]
    _check([x.get("pcm") for x in items] == [b"\x01", b"\x02", None, b"\x03"],
           "oldest audio dropped, order and control items kept")

    # Starter config: automatic activity detection, no manual turns, no compression.
    try:
        cfg = gemini_config("zh-Hans")
    except ImportError:
        cfg = None  # google-genai not installed: config check needs the SDK
    if cfg is not None:
        _check(cfg.realtime_input_config is None, "automatic activity detection stays on")
        _check(cfg.context_window_compression is None, "no context compression")
        _check(cfg.translation_config.target_language_code == "zh-Hans", "target passes through")
        _check(cfg.session_resumption is not None, "resumption configured")
        _check(cfg.system_instruction is None, "no instruction unless passed")
        _check(gemini_config("zh-Hans", instruction="x").system_instruction is not None,
               "instruction passes through")

    # System instruction: right roles and languages for every pair, nothing from
    # any other language leaking in, unknown codes fall back to their tag.
    codes = list(LANG_NAMES) + ["ta"]
    for spk, lis in (("doctor", "patient"), ("patient", "doctor")):
        for src in codes:
            for tgt in codes:
                if src == tgt:
                    continue
                si = system_instruction(spk, src, tgt)
                names = {LANG_NAMES.get(src, src), LANG_NAMES.get(tgt, tgt)}
                _check(f"hear only the {spk}, speaking {LANG_NAMES.get(src, src)}" in si,
                       f"{spk} {src}->{tgt}: speaker and source named")
                _check(f"to the {lis} in {LANG_NAMES.get(tgt, tgt)}" in si,
                       f"{spk} {src}->{tgt}: listener and target named")
                # English is named on purpose: code-switching and drug names.
                for other in set(LANG_NAMES.values()) - names - {"English"}:
                    _check(other not in si, f"{spk} {src}->{tgt}: {other} leaks in")
    try:
        system_instruction("nurse", "en", "zh")
    except ValueError:
        pass
    else:
        _check(False, "unknown speaker must fail")

    # Backend selection (no network: constructor arguments only).
    _check(client_kwargs("apikey", {"GEMINI_API_KEY": "k"}) ==
           {"api_key": "k", "http_options": {"api_version": "v1beta"}}, "apikey pins v1beta")
    _check(client_kwargs("enterprise", {"GOOGLE_CLOUD_PROJECT": "p"}) ==
           {"enterprise": True, "project": "p", "location": "us-central1"}, "enterprise = starter client")
    _check(backend_ready("apikey", {}) is not None, "apikey needs a key")
    _check(backend_ready("enterprise", {"GOOGLE_CLOUD_PROJECT": "p"}) is None, "enterprise needs a project")
    _old = os.environ.get("GEMINI_BACKEND")
    try:
        os.environ["GEMINI_BACKEND"] = "nope"
        try:
            backend()
        except ValueError:
            pass
        else:
            _check(False, "unknown backend must fail")
    finally:
        if _old is None:
            os.environ.pop("GEMINI_BACKEND", None)
        else:
            os.environ["GEMINI_BACKEND"] = _old

    # Language mapping.
    _check(lang_bcp47("zh") == "zh-Hans", "zh must map to zh-Hans")
    _check(lang_bcp47("en") == "en", "en passes through")
    _check(lang_bcp47("es") == "es", "es passes through")
    _check(lang_bcp47("fil") == "fil", "fil passes through")
    _check(lang_bcp47("zh-Hant") == "zh-Hant", "zh-Hant passes through")
    _check(lang_bcp47("pt-BR") == "pt-BR", "pt-BR passes through")

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
    parser = argparse.ArgumentParser(description="Interpret live translation server")
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument(
        "--streaming-mode",
        choices=STREAMING_MODES,
        default=None,
        help="default delivery mode for new sessions (overrides STREAMING_MODE)",
    )
    args = parser.parse_args()
    if args.self_check:
        _self_check()
    else:
        if args.streaming_mode is not None:
            DEFAULT_STREAMING_MODE = args.streaming_mode
        host = os.environ.get("HOST", "127.0.0.1")
        port = int(os.environ.get("PORT", "8000"))
        print(f"[interpret] listening on http://{host}:{port} ({DEFAULT_STREAMING_MODE} mode)")
        # Loopback by default: /ws is unauthenticated and every turn spends the
        # server-side API key, so exposing it on the LAN hands the key to anyone
        # who can reach the port. Set HOST=0.0.0.0 deliberately.
        uvicorn.run(app, host=host, port=port)
