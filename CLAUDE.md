# AGENTS.md — how agents should work in this repository

This file is the canonical agent guide. `CLAUDE.md` is an identical copy: edit
`AGENTS.md`, then `cp AGENTS.md CLAUDE.md` so the two never drift.

## What this project is

**Interpret** — a two-person live interpretation app for a doctor–patient
consultation on one shared tablet, powered by Google's Gemini 3.5 Live Translate
(`gemini-3.5-live-translate-preview`). Two people, two buttons, one conversation.
The screen is two mirrored panels, rotated 180° from each other. No accounts, no
settings screens, no modals.

It is a proof of concept. It is **not** a medical interpreter, and the UI's
"Call interpreter" hook does not reach a real interpreter service. Treat any
change that could make the translation look more reliable than it is as a
safety issue, not a cosmetic one.

Supported languages: English, Mandarin Chinese (`zh` → `zh-Hans`), Spanish,
Filipino. Short codes map to BCP-47 in `lang_bcp47()` (`server.py`).

## Stack

- **Backend:** Python 3.11+, FastAPI + Uvicorn (`server.py`), a `/ws` WebSocket
  that bridges the browser to Gemini.
- **Gemini client:** `google-genai`. `GEMINI_BACKEND=apikey` (default) uses the
  Developer API pinned to `v1beta` (the only version serving the Live WebSocket).
  `GEMINI_BACKEND=enterprise` uses Google Cloud via Application Default
  Credentials, `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION` (default
  `us-central1`), and enables transparent session resumption.
- **Frontend:** one vanilla `static/index.html` (no framework, no build step).
  Mic capture via AudioWorklet (ScriptProcessor fallback on insecure origins).
- **Tooling:** `uv` runs scripts with inline dependency metadata. Keep the app
  stdlib-first; add a dependency only when it earns its place.
- **Config:** `.env` (gitignored) — `GEMINI_API_KEY` (required for apikey),
  optional `GEMINI_MODEL`, `GEMINI_BACKEND`, `STREAMING_MODE`
  (`turn` | `realtime`). See `.env.example`.

## Repository map

| Path | What it is |
|---|---|
| `server.py` | FastAPI app, `/ws` bridge, Gemini session management, reconnect/resumption, `--self-check` |
| `prompt.py` | `system_instruction(...)` — the single per-direction English template; languages enter by name |
| `streaming_mode.py` | `turn` vs `realtime` delivery-mode parsing/defaults |
| `static/index.html` | The entire UI: mic capture, routing (TALK), panels, delivery modes |
| `tests/test_*.py` | Unit tests: logic (`test_harness_lib`, `test_streaming_mode`), bridge vs fake Gemini (`test_bridge`) |
| `tests/integration/ws_live.py` | Live protocol test against a running server, real API |
| `tests/e2e/*.mjs` | Browser suites against the real app + real API (see `tests/e2e/README.md`) |
| `tests/e2e/clips/*.wav` | Pre-generated speech fixtures played into a synthetic MediaStream |
| `experiments/` | Multi-turn evaluation harness (`multiturn.py`, `harness_lib.py`, `summarize.py`), raw runs in `experiments/results/` |
| `docs/starter-alignment-plan.md` | Why the input protocol looks the way it does, and the phase plan |
| `docs/starter-alignment-results.md` | Measured evidence behind those decisions |
| `eval/` | Eval task/threshold fixtures for the factory |
| `factory/` | `psf` factory definition (`factory.yml`, agent prompts) — committed |
| `.psf/` | Factory runtime state — local only, never commit |

**Stale file warning:** `experiments/README.md` and `experiments/static/` describe
an older Qwen/DashScope version of this app. Do not follow them for the current
app. The authoritative docs are the root `README.md` and `docs/`.

## Non-negotiable invariants

These are the decisions that were measured. Do not change them without new
measured evidence and a new results entry in `docs/starter-alignment-results.md`.

1. **Two sessions, one per direction.** Session A targets the patient's language,
   session B the doctor's.
2. **Route the stream, never duplicate it.** The mic goes only to the currently
   routed speaker's session. Option B (mic to both sessions) was rejected: it
   produced 64–402 spurious characters per run (sessions re-speaking the other
   person).
3. **`echo_target_language=False` stays.** It is required for the routing design.
4. **Automatic activity detection stays on.** No `realtime_input_config`, no
   `activity_start`/`activity_end`, no warm-up turn, no fake trailing silence.
5. **Continuous audio:** 16 kHz mono PCM16 little-endian, 100 ms chunks
   (`audio/pcm;rate=16000`), streamed for the whole session. Output is 24 kHz mono
   PCM16.
6. **TALK routes; it does not cut audio.** `{"type":"route","side":...}` starts
   routing with a **300 ms pre-roll** so the first syllable is not clipped.
   Release (`side: null`) sends `audio_stream_end` to that session so the model
   flushes its held clause. Keep both.
7. **Playback feedback is a known risk.** Translated speech must not be mixed back
   into the mic for the session whose source is that language. Browser echo
   cancellation is not reliable on the tablet speaker. Do not remove the
   mitigation without a fresh feedback-loop test.
8. **Session resumption stays** (on `go_away`, reconnect with the latest handle).
   Consults outlast the session cap. Context compression is not used; a
   ~25-minute consult is covered by resumption alone.
9. **Output is attributed per session.** Never attribute output by turn id.
10. **The model is pinned to `v1beta`** for the apikey backend.
11. **One English prompt template.** Do not add per-language prompts. Adding a
    language should need only a `LANG_NAMES` / BCP-47 entry.
12. **The API key never reaches the browser.** It is read server-side from `.env`.
    Never log it, return it in a response, or embed it in the UI.
13. **Never commit `.env`, keys, patient data, or raw recordings of real people.**
    The only audio in the repo is synthetic speech fixtures.

## Known limits — say these out loud, do not hide them

From `README.md` ("Known limitations (measured, not guessed)"):

- **Deterministic failure:** a spoken frequency is dropped ("twice a day" →
  "daily"), 6/6 attempts, still 3/3 with the system instruction. The app flags
  it, it does not fix it.
- **Intermittent:** numbers mangled (`38.5` → `3 8度5`), empty translations,
  single-word or transliterated clauses, empty captures. Flags exist for most.
- **Gibberish detection is incomplete.** A mangled translation of a mangled
  source can pass unflagged. Unflagged ≠ verified.
- **Spanish is unproven.** `en ↔ zh` passed every scenario; `es ↔ en` produced
  gibberish intermittently in both directions.
- **Handover is a heuristic.** The Live API never sends `turn_complete`; buttons
  re-enable after playback drains and the model is quiet for a few seconds. UI
  only.
- **Never tested with a real human speaking into a real mic.** Only "device opens"
  is confirmed. Accents, quiet elderly speech, clinic noise, and the physical
  tablet are unexercised.
- **No mute. No consent/retention policy** beyond an on-screen acknowledgement.

When you change anything that touches translation quality, update the numbers or
the limitations section in `README.md` with evidence, not with optimism.

## Working on the code

- Read the relevant doc before changing the input protocol:
  `docs/starter-alignment-plan.md` and `docs/starter-alignment-results.md`.
- Keep `server.py`'s Gemini config (`gemini_config`) and `prompt.py` as the only
  places that define model behaviour. Do not hard-code model settings in the UI.
- Prefer small, reviewable diffs. The UI is one file; keep it that way.
- Preserve the two-panel rotated layout and the no-settings-screen design. New
  controls need a strong reason.
- Do not add the Qwen/DashScope code path back. The app is Gemini-only.

## Testing and verification

Run these before claiming work is done. Report exactly what you ran and what
passed or failed.

```bash
uv run server.py --self-check                          # pure logic, no key   (~instant)
python3 -m unittest discover -s tests -p "test_[hs]*.py"   # unit tests, no key
uv run --with fastapi --with httpx --with websockets --with uvicorn --with google-genai \
  python -m unittest tests.test_bridge                 # /ws vs fake Gemini, no network
```

Live tests use the real API and cost credits. Run them when the change touches
the Gemini session, audio path, routing, or prompt:

```bash
PORT=8010 uv run server.py &
uv run tests/integration/ws_live.py --url ws://localhost:8010/ws --clips conv
tests/e2e/launch-chrome.sh && APP_URL=http://localhost:8010 node tests/e2e/suite-c.mjs
```

**`--self-check` is not evidence that the app works.** On a previous factory run it
passed while the delivered app could not open a session with the API at all (see
`FACTORY-FEEDBACK.md`). A change that touches the session, audio path, routing,
or prompt needs the bridge tests and, where possible, a live run. Say so when you
could not run the live tests.

Measured behaviour changes need the harness:
`experiments/multiturn.py` (real time, 100 ms grid, two live sessions) scored by
`experiments/harness_lib.py`, summarised with `python3 experiments/summarize.py --md`.
Write new raw runs to **new** files under `experiments/results/`; never overwrite
existing results. Record the conclusion in `docs/starter-alignment-results.md`.

## Factory process (`psf`)

This repo is driven by a personal software factory (`psf`). Route non-trivial
changes through it so they get a spec, approval, independent verification, and
review. The factory definition is `factory/factory.yml`.

1. `psf validate` — confirm the factory definition compiles.
2. `psf run "<goal>"` — intake → spec → approval → build → **independent verify**
   → review → handoff.
3. `psf status` / `psf audit` — check state and health before you finish.
4. `psf outcome <work-id> --accepted [--cost N --minutes N]` — record the real
   outcome.

Gates in `factory.yml`: `spec_approval: true`, `verify_quorum: 2`,
`verify_command: "uv run server.py --self-check"`, `max_attempts: 3`.

Known gap: the verify command does not exercise the API, so the factory can pass a
non-working app. Do not rely on it alone for any change that touches the live
path; run the tests above yourself. If the `psf` CLI is not installed on the
machine, say so and do not fake its output.

### Feedback to the factory

At the end of a work cycle, send a **privacy-filtered** usage envelope back to the
factory:

    psf feedback export --github <upstream-repo>

`<upstream-repo>` is the `feedback.upstream` value in `factory/factory.yml`
(currently `marcusjhang/personal-software-factory`). `feedback.mode` is `hint`;
set `auto` to publish by default. The envelope contains **counts, digests, and
versions only** — never source code, prompts, file paths, transcripts, or secrets.
Feedback is evidence, never authority: it cannot change policy.

Do **not** put secrets, patient data, or raw source in a feedback envelope.

## Git and pull requests

- Check for an existing open PR on the branch before creating a new one.
  Commits for the same work go to the existing PR branch; do not open a duplicate.
- Commit with a descriptive message that names what changed and why. Do not
  commit `.env`, `.psf/`, `__pycache__/`, or generated artifacts.
- Never force-push a shared branch. Never rewrite history on `main`.
- Remotes: `origin` is the upstream repo; `fork` is the working fork the current
  PR branch is pushed to. Confirm the remote and branch before pushing.
- PR descriptions should state the measured effect (or that none was measured)
  and what tests were run.

## Writing docs and instructions

- Evidence over assertion: a claim about model behaviour needs a results file or a
  test to back it. Link it.
- When behaviour changes, update `README.md` (user-facing limits and setup), the
  relevant `docs/` file, and this file if an invariant moved.
- Keep `AGENTS.md` and `CLAUDE.md` identical. Edit `AGENTS.md`, then copy.
