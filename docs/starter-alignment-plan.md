# Plan: align the app with the Live Translate starter

**Goal.** Feed the model the way Google's starter does — one continuous, real-time
16 kHz mic stream, automatic activity detection on, no hand-made turns — so translation
is as natural and smooth as the model allows, and the stack of workarounds that manual
turns forced on us can be deleted.

**Why.** A single-turn test (`experiments/starter_repro.py`) showed the model behaves the
same in manual and auto mode *for one turn*. The problems we have fought — dropped final
clause, cold first turn, late output landing on the wrong turn, sessions closing between
turns — all live in what manual, gated input does *across* turns. Each workaround patches
a symptom of the one before it.

**Process.** Per `AGENTS.md`, the implementation phases (2–5) go through `psf run`, one
goal per phase, so each gets a spec, independent verify and review. Phase 1 is
experiment-only (`experiments/`) and decides between options A and B before any app
code changes.

---

## Current state vs target

| Concern | Now | Target |
|---|---|---|
| Activity detection | disabled; `activity_start`/`activity_end` per turn (`server.py:124-126, 217-222`) | automatic (starter default) |
| Mic upload | only while a turn is open; cut after 1.2 s below RMS 0.01 (`index.html:1046, 1063-1067`) | every 100 ms chunk, for the whole session |
| Turn end | auto after 3 s quiet (`index.html:1073`); server adds 800 ms fake silence (`server.py:412-418`) | none — the model segments speech itself |
| Cold start | warm-up silent turn (`server.py:193-206`), `COLD_GRACE_MS` | none (re-add only if Phase 1 shows cold starts persist) |
| Output attribution | per-turn tag `cur_turn`, `lineByTurn`, drop on mismatch (`server.py:180-184, 257`; `index.html:719-733`) | per-session `side` only |
| Turn-done detection | `QUIESCE_MS`, `startDrain()`, `TURN_HARD_CAP_MS`, `MAX_TURN_MS` | none; transcript lines segmented by pause |
| Context compression | sliding window 16k tokens | removed, *if* the longevity test passes without it |
| Resumption | handle-based, reconnect on `go_away` | **kept** (consults outlast the session cap) |
| `echo_target_language` | `False` | **kept** (deliberate; required for option B) |
| Sessions | two, one per direction | **kept** |
| Audio formats | 16 kHz in / 24 kHz out / 100 ms / `audio/pcm;rate=16000` | **already compliant** |
| Backend | Developer API key, `v1beta` | unchanged; `enterprise=True` evaluated in Phase 5 |

---

## Decision to make in Phase 1: how audio reaches a session

**A — button routes, mic always streams.** The talk button only chooses which session
receives the live mic. The other session receives paced silence (or nothing — Phase 1
decides). Keeps explicit control; smallest behavioural change for users.

**B — no button; mic goes to both sessions.** With `echo_target_language=False`, the
doctor→zh session should ignore Mandarin and the patient→en session should ignore
English, so each session translates only its own speaker. Literally the starter, twice.
Removes the most code. Risk: crosstalk (a session translating the wrong speaker,
especially with code-switching or loan words like drug names).

**Both options share one new risk — playback feedback.** Once the mic streams
continuously, the tablet speaker's translated audio reaches the mic. Example: the
Mandarin translation of the doctor plays; the patient→en session hears Mandarin and
translates it back to English. Browser echo cancellation reduces this but is not
reliable on a tablet speaker. Mitigation (designed in Phase 3, step 3.4): while
translated audio in language *L* is playing, the session whose **source** is *L*
receives silence instead of the mic.

---

## Phase 1 — Evidence (experiments only, no app changes)

Extend `experiments/starter_repro.py` into a multi-turn harness.

1. **Inputs.** `--clips a.wav,b.wav,...` played in order through *one* session, with
   `--gap SECONDS` of input between clips. Expected text per clip comes from
   `tests/e2e/gen-clips.py` (import its table rather than duplicating it).
2. **Input modes** (`--input`):
   - `continuous` — starter: every 100 ms chunk sent, gaps filled with low-level noise
     (a real mic is never digital zero; also test pure zero to settle the claim in
     `index.html:399-401`).
   - `gated` — reproduces today's app: send speech + 1.2 s tail, nothing during gaps,
     800 ms paced silence + `activity_end`, manual VAD.
3. **Two-session modes** (`--sessions`):
   - `routed` (option A): doctor clips → session 1, patient clips → session 2; the idle
     session gets `--idle silence|nothing`.
   - `both` (option B): every clip to both sessions.
4. **Feedback simulation** (`--feedback`): mix the received translation audio back into
   the input stream at −20 dB, with and without the step-3.4 suppression rule.
5. **Measure per clip** and print a table:
   - completeness: share of expected words present in the output transcript
     (last-clause dropped = yes/no)
   - latency: speech-end → first output audio, speech-end → last output text
   - attribution: output arriving after the next clip started (late bleed)
   - crosstalk (option B): any output from the wrong session
   - session events: `go_away`, clean closes, reconnects, `turn_complete` seen
6. **Run matrix** (≥3 runs each, the model is nondeterministic): the `turn-*`
   conversation clips and the `c-*` doctor/patient pairs, gaps of 1 s, 5 s, 30 s.
7. **Exit criteria / decision.**
   - Continuous beats or equals gated on completeness and latency → proceed.
   - Option B crosstalk rate 0 over the matrix, including code-switched clips → choose B;
     otherwise A.
   - A with `--idle nothing` shows no idle closes or cold starts at 30 s gaps → drop idle
     silence; otherwise the server feeds paced silence to the idle session.
   - Record results in `docs/starter-alignment-results.md`.

## Phase 2 — Server (`server.py`)

One `psf run` goal. Changes:

1. **Config** (`gemini_config`): delete `realtime_input_config`. Delete
   `context_window_compression` (restored in Phase 4 only if the longevity test fails).
   Keep `session_resumption` and `echo_target_language=False`.
2. **Send loop** (`_direction`): accept only `audio` and `close`. Delete the
   `activity_start`, `activity_end` and `silence` branches and the warm-up block.
3. **Recv loop**: tag output with `side` only; delete `cur_turn`. Keep go_away/handle
   handling and the reconnect loop unchanged.
4. **Wire protocol** (browser → server), replacing `talk_start`/`talk_end`:
   - `{"type": "audio", "b64": ...}` — always.
   - Option A: `{"type": "route", "side": "doctor"|"patient"}`; server forwards mic audio
     to that side's queue. If Phase 1 chose idle silence, a 100 ms ticker task feeds
     silence to the other queue.
   - Option B: no route message; server puts every chunk on both queues.
   - Suppression (step 3.4) is decided client-side and sent as
     `{"type": "suppress", "side": ..., "on": bool}`; the server substitutes silence for
     that side while on. (Keeping the decision in the browser keeps it next to playback
     timing.)
5. **Normaliser**: keep `normalize_transcript`; `turn_end` stays (forwarded if the model
   ever sends `turn_complete`) but nothing depends on it.
6. **Self-check** (`_self_check`, the factory's `verify_command`): add checks for the
   new pure pieces — routing (which queues a chunk goes to for A/B/suppress) extracted
   as a pure function `route_chunk(mode, active, suppressed) -> list[side]`.
7. **Reconnect backlog**: while a direction reconnects, its queue keeps filling with
   live audio. Bound it (drop oldest beyond ~2 s) so a reconnect doesn't replay stale
   speech in a burst.

## Phase 3 — Browser (`static/index.html`)

One `psf run` goal (can start once Phase 2's protocol is fixed).

1. **Capture** — replace `createScriptProcessor(2048)` with an `AudioWorklet` that
   emits exactly 1600 samples (100 ms) per message, converting float → Int16 LE.
   Keep `getUserMedia` constraints (16 kHz, mono, AEC, NS). Start the mic when the
   session starts, not per turn; keep the `audioCtx.resume()` fix inside the Start
   gesture.
2. **Upload** — send every chunk while the socket is open. Delete the `phase` early
   return, `TRAIL_SILENCE_MS` gating, `SILENCE_THRESHOLD`/`heardSound`/`lastSound`, and
   the 3 s auto-end.
3. **Turn machinery** — delete `startTalk`/`stopTalk` turn semantics, `turnSeq`,
   `currentTurn`, `lineByTurn`, `startDrain`, `closeTurn`, `QUIESCE_MS`,
   `COLD_GRACE_MS`, `TURN_HARD_CAP_MS`, `MAX_TURN_MS`, `turnCapTimer`, `sawOutput`.
   - Option A: the panel button becomes a route toggle (`route` message); UI shows who
     the mic is routed to.
   - Option B: buttons removed; both panels show live "listening" state.
4. **Feedback suppression** — when scheduling a loud output chunk from session *S*
   (which speaks *S*'s target language), send `suppress` for the *other* session (whose
   source is that language) until `speechEnd + 300 ms`, then release. Unit-testable as a
   pure function of (now, speechEnd, side).
5. **Transcript segmentation** — no turns, so lines are built per side:
   append `source`/`translation` text to that side's open line; start a new line when
   that side has produced no text for `LINE_GAP_MS` (start at 1500, tune from Phase 1
   data) or, in option A, when the route changes.
6. **Playback** — keep `scheduleAudio` (loud/padding split, `PLAYOUT_LEAD_S`, queue
   cap). Accept audio from any side (no `turn`/`active` filter), scheduling each side on
   the shared timeline.
   - **Delivery modes** (`streaming_mode`): `realtime` maps directly. `turn` ("speak
     after the speaker finishes") is redefined as: buffer a side's audio until its line
     closes by `LINE_GAP_MS`, then play. Confirm this product behaviour before building.
7. **Debug recorder** (`?debugaudio`) — record per line instead of per turn; keep the
   three files (sent / received / played).
8. **Errors / end / retry** — `onError`, `endSession`, `retryAll` lose their turn-timer
   cleanup; `endSession` also stops the worklet.

## Phase 4 — Tests

1. **Unit / self-check** — new pure functions (routing, suppression window, line
   segmentation) covered in `_self_check` and `tests/`.
2. **E2E harness** — the synthetic `MediaStream` must keep producing low-level noise
   between clips (today it only needs to play during a turn). Update the injector first.
3. **E2E suites** — rewrite assertions that assume tap → turn → close:
   `conversation.mjs`, `check-end.mjs`, `suite-a.mjs`, `suite-b.mjs`,
   `number-integrity.mjs`, `disconnect.mjs`, `misuse.mjs`. Replace "turn closed"
   waits with "line for side X stable for `LINE_GAP_MS`".
4. **New E2E cases** —
   - long pause mid-sentence (2–4 s) still yields one complete translation
   - quiet speaker (clip at −25 dB) is translated, not dropped
   - feedback: translation playback is not re-translated by the other session
   - option B: crosstalk check on code-switched clips
5. **Longevity** (`longevity.mjs`) — run a >20 min consult without
   `context_window_compression`. If sessions die early, restore compression and record
   why in a comment.
6. **Regression comparison** — run the full e2e set on `main` and on the branch; the
   branch must be ≥ main on completeness and ≤ main on latency.

## Phase 5 — Backend parity (optional, needs GCP)

1. `gcloud auth application-default login`; set `GOOGLE_CLOUD_PROJECT`,
   `GOOGLE_CLOUD_LOCATION`.
2. Run the Phase 1 matrix with `--backend enterprise`.
3. If it is measurably better, add `GEMINI_BACKEND=apikey|enterprise` to
   `gemini_client()`; with `enterprise`, `SessionResumptionConfig(transparent=True)`
   becomes available and the handle juggling can simplify.

## Phase 6 — Cleanup and docs

- Delete dead constants, comments that justify removed workarounds, and the
  `turn`-based wording in `README.md`; document the input model (continuous stream,
  auto VAD, routing/suppression) and the audio spec.
- `psf status`, `psf audit`, `psf outcome <work-id> --accepted`, then
  `psf feedback export` per `AGENTS.md`.

---

## Risks

| Risk | Where it shows | Mitigation |
|---|---|---|
| Playback re-translated (feedback loop) | both options, worst on tablet speaker | step 3.4 suppression + AEC; e2e feedback case |
| Crosstalk in option B | wrong-side output, drug names, code-switching | Phase 1 gate; fall back to A |
| Idle session closes / cold start (option A) | 30 s gaps | Phase 1 decides idle silence ticker |
| Session cap without compression | consults > ~15 min | longevity test; restore compression |
| Reconnect replays stale audio | burst after `go_away` | bounded queue (step 2.7) |
| More upstream audio = more cost | continuous streaming bills silence too | measure per-consult usage in Phase 1; acceptable for a POC, revisit for prod |
| "Turn" delivery mode semantics change | users expecting speak-then-play | confirm 3.6 behaviour with product owner |

## Acceptance criteria

- No `activity_start`/`activity_end`, no `realtime_input_config`, no warm-up, no fake
  silence, no turn-tagging in `server.py`.
- Browser sends a 100 ms chunk every 100 ms for the whole session (verified with
  `?debugaudio`: `sent` file has no gaps).
- Phase 1 matrix and e2e suites: no dropped final clause, no late-output misattribution,
  no cold-start empty first line, completeness ≥ main, latency ≤ main.
- `uv run server.py --self-check` passes; `psf audit` clean.

## Order and sizing

| Phase | Depends on | Size |
|---|---|---|
| 1 Evidence | — | ~1 day (mostly API run time) |
| 2 Server | 1 | small |
| 3 Browser | 1, protocol from 2 | medium (largest diff) |
| 4 Tests | 2, 3 | medium |
| 5 Enterprise | GCP access | small, optional |
| 6 Cleanup | 4 | small |
