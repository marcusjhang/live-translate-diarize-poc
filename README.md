# Interpret

A simple, turn-based, two-person live interpretation app for a doctor–patient
consultation on one shared tablet, powered by Gemini 3.5 Live Translate.

Two people, two buttons, one conversation. No accounts, no settings screens, no
modals. The screen is split into two mirrored panels rotated 180° from each
other, so each person reads their own panel from their side of the desk.

## Setup

```bash
cp .env.example .env          # fill in GEMINI_API_KEY
uv run server.py              # http://localhost:8000
```

The key is held server-side and never reaches the browser.

Self-check the pure logic with no key set:

```bash
uv run server.py --self-check
```

## Languages

English, Mandarin Chinese (`zh` → `zh-Hans`), Spanish, Filipino. Map short
codes to BCP-47 (`zh` → `zh-Hans`) in `lang_bcp47()`.

## Turn protocol

The API has no turn concept — it streams the translation progressively while the
person is still speaking. Turn-based delivery is the app's job. The Gemini Live
Translate session is configured to:

- `response_modalities=[AUDIO]` — text arrives via the transcription configs,
  not as a text modality.
- `translation_config(target_language_code=<other BCP-47>, echo_target_language=False)`
  — `False` is required; `True` makes the model parrot same-language input.
- `input_audio_transcription` / `output_audio_transcription` — source text and
  translated text.
- `realtime_input_config(automatic_activity_detection=disabled)` — server-side
  VAD off; the app drives turns explicitly.
- `session_resumption(transparent=True)` + `context_window_compression(...)` —
  live sessions have a ~10 min connection cap and a ~15 min audio-only cap and
  emit `go_away` before terminating. On `go_away` the app reconnects using
  `session_resumption_update.new_handle`.

Audio in: 16 kHz mono PCM16 little-endian in 100 ms chunks
(`audio/pcm;rate=16000`). Audio out: 24 kHz mono PCM16.

Per turn:

1. **TALK tap-down** — `session.send_realtime_input(activity_start=ActivityStart())`.
2. Stream 100 ms audio chunks.
3. **TALK tap-up** — send ~800 ms of silence as audio chunks, *then*
   `activity_end=ActivityEnd()`. This is the single most important detail:
   400 ms of trailing silence truncates the final clause; 800 ms yields the
   complete sentence.
4. On `go_away`, reconnect using the resumption handle.

## Two sessions per direction

Two directions need two sessions: session A targets the patient's language and
is fed the doctor's mic; session B targets the doctor's language and is fed the
patient's mic. Both are opened at start and kept warm so the first turn is fast.
Only one direction may stream at a time.

## App-side buffering

The model streams the translated audio while the speaker is still talking. The
app buffers that audio (and holds the translated text) and releases it only when
the turn closes, so the listener never hears a partial, overlapping translation.
While LISTENING the speaker's own words stream live in both panels — the "did it
hear me" reassurance — while the translated line appears after the turn closes.

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
| A spoken **frequency** is dropped in translation (e.g. "twice a day" -> "daily") | 6 / 6 attempts — **deterministic** | yes, `Check the number (2)` |
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

- **Turn ending is a heuristic.** The Live API never sends `turn_complete` or
  `generation_complete`; a turn is closed when playback has drained and the model
  has been quiet for a few seconds.
- **Output is attributed by arrival time.** If the model pauses more than ~3s
  mid-answer, a clause can land in the following turn's transcript line.
- **A 10-minute session cap is real** and handled: the server reconnects with the
  resumption handle. A 60-turn, 11.6-minute session completed without loss.
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
