# Interpret

A simple two-person live interpretation app for a doctor–patient
consultation on one shared tablet, powered by Gemini 3.5 Live Translate.

Two people, two buttons, one conversation. No accounts, no settings screens, no
modals. The screen is split into two mirrored panels rotated 180° from each
other, so each person reads their own panel from their side of the desk.

## Setup

```bash
cp .env.example .env          # fill in GEMINI_API_KEY
uv run server.py              # http://localhost:8000
```

Translation is delivered after each turn by default. Start in progressive live
mode with either `STREAMING_MODE=realtime uv run server.py` or
`uv run server.py --streaming-mode realtime`. The in-session `Mode` control
switches between `After turn` and `Live` without opening a settings screen.

The key is held server-side and never reaches the browser.

### Backend

`GEMINI_BACKEND=apikey` (default) uses the Developer API with `GEMINI_API_KEY`,
pinned to `v1beta` (the only version that serves the Live WebSocket).
`GEMINI_BACKEND=enterprise` uses Google Cloud exactly like Google's starter,
`genai.Client(enterprise=True, project=..., location=...)`, with Application
Default Credentials (`gcloud auth application-default login`),
`GOOGLE_CLOUD_PROJECT` and optional `GOOGLE_CLOUD_LOCATION` (default
`us-central1`); it also enables transparent session resumption.

### Tests

```bash
uv run server.py --self-check                     # pure logic, no key
python3 -m unittest discover -s tests -p "test_[hs]*.py"
uv run --with fastapi --with httpx --with websockets --with uvicorn --with google-genai \
  python -m unittest tests.test_bridge            # /ws bridge vs a fake Gemini, no network
PORT=8010 uv run server.py &                      # live tests below use the real API
uv run tests/integration/ws_live.py --url ws://localhost:8010/ws --clips conv
tests/e2e/launch-chrome.sh && APP_URL=http://localhost:8010 node tests/e2e/suite-c.mjs
```

See `tests/e2e/README.md` for the browser suites and `experiments/` for the
multi-turn harness behind `docs/starter-alignment-results.md`.

## Languages

English, Mandarin Chinese (`zh` → `zh-Hans`), Spanish, Filipino. Map short
codes to BCP-47 (`zh` → `zh-Hans`) in `lang_bcp47()`.

## Input protocol

The model is fed exactly the way Google's Live Translate starter feeds it, once
per direction (`docs/starter-alignment-plan.md`, evidence in
`docs/starter-alignment-results.md`):

- `response_modalities=[AUDIO]`, `input_audio_transcription`,
  `output_audio_transcription`, `translation_config(target_language_code=<other
  BCP-47>, echo_target_language=False)`.
- `system_instruction` per direction (`prompt.py`): GP-consult interpreter role,
  who is speaking to whom, keep every number/dose/frequency/negation, keep drug
  names untranslated, polite register. One English template for every language
  pair; languages enter only by name (unknown codes by their BCP-47 tag), so adding
  a language needs no prompt change. Measured en↔zh, 3 runs each
  (`experiments/results/*_si{off,on}.*`): fixes `38.5` → "38 degrees" (0/3 → 3/3) and
  the dropped "come back" clause in turn-d5 (0/3 → 3/3); does **not** fix "twice a
  day" (wrong 3/3 either way); first-audio latency unchanged (~2.6 s).
- **Automatic activity detection stays on.** No `realtime_input_config`, no
  `activity_start`/`activity_end`, no warm-up turn, no fake trailing silence.
- `session_resumption` — on `go_away` the server reconnects with the latest
  handle (transparent resumption on the enterprise backend). No context
  compression: a 60-turn, ~25-minute consult is covered by resumption alone.

Audio in: one continuous stream of 16 kHz mono PCM16 little-endian, 100 ms chunks
(`audio/pcm;rate=16000`), captured by an AudioWorklet (ScriptProcessor fallback on
insecure origins) and sent for the whole session. Audio out: 24 kHz mono PCM16.

TALK does not cut the audio up; it **routes** the stream:

1. **TALK** — `{"type":"route","side":...}`. The server forwards the stream to
   that person's session, starting with the last 300 ms of mic audio (pre-roll) so
   the first syllable is not clipped.
2. **Release** (tap again, or 3 s of quiet after speech) — `{"type":"route",
   "side":null}`. The server sends `audio_stream_end` to that session, which makes
   the model flush the clause it is still holding (translation reorders, so the
   final clause is otherwise late or lost).
3. While translated speech plays and nobody holds the mic, the browser stops
   uploading, so the pre-roll never contains the translation itself.

## Two sessions, one route

Session A targets the patient's language and session B the doctor's. Only the
routed session receives audio: `echo_target_language=False` does **not** stop a
session re-speaking input already in its target language, so feeding the mic to
both sessions makes each one parrot the other person. Output is attributed by
session — whatever session A says belongs to the doctor's latest line, however
late it arrives.

## Delivery modes

The model streams translated text and audio while the speaker is still talking.
In the default `turn` mode, the app buffers translated audio and holds translated
text on screen until the turn closes, so the listener never hears a partial,
overlapping translation. In `realtime` mode, the app renders text deltas and
plays audio chunks as they arrive. Source speech remains visible live in both
modes as the "did it hear me" reassurance.

## Disclaimer

`Call interpreter` is a defined hook; there is no real interpreter-service
integration. Automatic translation is not a medical interpreter.

---

## Known limitations (measured, not guessed)

Tested against the live API with synthesised speech. Numbers below are from an
observed test run, not a guarantee — the model is stochastic and results vary.

### The tool flags, but cannot fix, these

| Failure | Frequency | Detected? |
|---|---|---|
| A spoken **frequency** is dropped in translation (e.g. "twice a day" -> "daily") | 6 / 6 attempts — **deterministic**; still 3 / 3 with the system instruction ("一天吃两片/一片") | yes, `Check the number (2)` |
| A **number** is mangled (`38.5` -> `3 8度5`) | intermittent | yes, `Check the number` |
| A turn produces **no translation** at all | intermittent | yes, `No translation — please check` |
| A clause is rendered as **one bare word** / transliterated gibberish (`Me duele la cabeza.` -> `Midway`) | intermittent | partly — see below |
| A turn captures **nothing** at all | rare | yes, `Nothing captured — please say again` |

**Gibberish detection is incomplete.** Two guards exist (a whole clause collapsing
to one word, and a missing number). A mangled translation with a mangled *source*
slips past both — observed once as `Me dole lá,`. Treat any flagged turn as
untrustworthy and any *unflagged* turn as probably-right, not certainly-right.

### Language pairs are not equally reliable

`en <-> zh` passed every scenario. `es <-> en` failed intermittently, producing
gibberish in both directions. Spanish should be considered **unproven**.

### Structural limits

- **Handover is a heuristic.** The Live API never sends `turn_complete`; the
  buttons re-enable when playback has drained and the model has been quiet for a
  few seconds. This affects the UI only, never what the model hears.
- **Output is attributed by session**, so a late clause can only land on the same
  person's latest line, never on the other person's.
- **The session cap is real** and handled: the server reconnects with the
  resumption handle.
- **The microphone is opened when the session starts**, not on first tap, because
  creating the audio context on demand raced the speaker's first words and lost
  whole opening turns.

### Not built

No mute. No consent/retention policy beyond an on-screen acknowledgement.
"Call interpreter" explains that this tool cannot summon a human; it does not.

### Never tested

A real microphone has only been confirmed to *open* (device present, permission
granted, no error). No real human has spoken into it: accents, quiet elderly
speech, background clinic noise, and the physical tablet layout are all
unexercised. Echo cancellation is enabled and will suppress speaker output by
design, so the audio path cannot be verified by playing sound at the device.
