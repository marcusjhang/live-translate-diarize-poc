# Phase 1 results: continuous (starter-style) vs gated input

Harness: `experiments/multiturn.py` (two live sessions, real time on a 100 ms grid,
real Gemini 3.5 Live Translate, Developer API `v1beta`). Scoring:
`experiments/harness_lib.py`; table regenerated from raw events by
`python3 experiments/summarize.py --md`. Raw runs: `experiments/results/`.

Conditions are named `<input>_<clips>_g<gap seconds>`:

- `gated` — today's app (manual VAD, activity markers, warm-up, 1.2 s mic tail, 800 ms fake silence).
- `routed` — option A, continuous stream to the current speaker's session; `noise`/`nothing`/`zero` = what the idle session receives; `se` = `audio_stream_end` on release.
- `both` — option B, mic to both sessions.
- `fb` = speaker playback mixed back into the mic at −20 dB; `sup` = naive suppression.

Columns: `zh_recall`/`en_recall` = share of spoken words the model's input transcript
contains; `OUT_tail_drop` = clips whose translation lost the final clause;
`spurious` = translated characters attributed to nobody (crosstalk/echo);
`late` = output arriving after the next speaker started; times in seconds from clip start.

| condition | runs | zh_recall | zh<0.9 | en_recall | in_recall | in_tail_drop | OUT_tail_drop | empty | late | spurious | first_audio | last_out | closes |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| both_conv4_g30 | 2 | 0.594 | 3/4 | 1.0 | 0.797 | 3/8 | 0/8 | 0 | 0 | 166 | 2.26 | 1.5 | 0 |
| both_conv_g1 | 2 | 0.725 | 6/10 | 1.0 | 0.863 | 4/20 | 0/20 | 0 | 271 | 64 | 1.65 | 1.2 | 0 |
| both_conv_g5 | 2 | 0.975 | 2/10 | 1.0 | 0.988 | 2/20 | 0/20 | 0 | 15 | 402 | 2.92 | 1.46 | 0 |
| both_fb_conv_g1 | 2 | 0.664 | 6/10 | 1.0 | 0.832 | 4/20 | 2/20 | 0 | 385 | 103 | 1.19 | 2.71 | 0 |
| both_fbsup_conv_g1 | 2 | 0.142 | 10/10 | 0.444 | 0.293 | 17/20 | 17/20 | 0 | 317 | 117 | 0.21 | 3.87 | 0 |
| both_hard_g5 | 2 | 0.952 | 2/6 | 1.0 | 0.976 | 2/12 | 0/12 | 0 | 0 | 72 | 2.71 | 1.6 | 0 |
| gated_conv4_g30 | 2 | 0.938 | 2/4 | 1.0 | 0.969 | 2/8 | 0/8 | 0 | 0 | 0 | 2.46 | 1.35 | 0 |
| gated_conv_g5 | 5 | 0.939 | 9/25 | 1.0 | 0.97 | 9/50 | 6/50 | 0 | 0 | 0 | 2.57 | 1.34 | 0 |
| gated_hard_g5 | 5 | 0.952 | 5/15 | 1.0 | 0.976 | 5/30 | 5/30 | 0 | 0 | 0 | 2.02 | 1.42 | 0 |
| routed_fb_conv_g1 | 2 | 0.95 | 2/10 | 1.0 | 0.975 | 2/20 | 2/20 | 0 | 244 | 47 | 2.81 | 2.05 | 0 |
| routed_fbsup_conv_g1 | 2 | 0.0 | 10/10 | 0.991 | 0.495 | 10/20 | 11/20 | 10 | 41 | 0 | 3.05 | 1.57 | 0 |
| routed_hard_g5 | 5 | 0.915 | 10/15 | 0.973 | 0.944 | 5/30 | 0/30 | 0 | 0 | 0 | 2.85 | 1.71 | 0 |
| routed_noise_conv4_g30 | 2 | 0.8 | 3/4 | 1.0 | 0.9 | 2/8 | 0/8 | 0 | 0 | 0 | 2.62 | 1.56 | 0 |
| routed_noise_conv_g5 | 5 | 0.955 | 9/25 | 0.985 | 0.97 | 9/50 | 7/50 | 0 | 0 | 0 | 2.52 | 1.29 | 0 |
| routed_nothing_conv4_g30 | 2 | 0.825 | 2/4 | 1.0 | 0.912 | 2/8 | 1/8 | 0 | 0 | 0 | 2.71 | 1.41 | 0 |
| routed_nothing_conv_g5 | 5 | 0.95 | 5/25 | 1.0 | 0.975 | 5/50 | 0/50 | 0 | 0 | 0 | 2.6 | 1.3 | 0 |
| routed_se_conv4_g30 | 3 | 0.804 | 5/6 | 0.985 | 0.895 | 3/12 | 0/12 | 0 | 0 | 0 | 2.51 | 1.55 | 0 |
| routed_se_conv_g2 | 3 | 0.953 | 5/15 | 1.0 | 0.976 | 5/30 | 6/30 | 0 | 0 | 0 | 1.01 | 1.15 | 0 |
| routed_se_conv_g5 | 3 | 0.967 | 4/15 | 1.0 | 0.984 | 4/30 | 3/30 | 0 | 0 | 0 | 1.64 | 1.31 | 0 |
| routed_se_hard_g5 | 3 | 0.928 | 5/9 | 1.0 | 0.964 | 3/18 | 2/18 | 0 | 0 | 0 | 1.42 | 1.5 | 0 |
| routed_zero_conv_g5 | 2 | 0.955 | 2/10 | 1.0 | 0.978 | 2/20 | 3/20 | 0 | 0 | 0 | 2.79 | 1.32 | 0 |

## Decisions

1. **Option B is rejected.** `echo_target_language=False` does **not** stop a session
   re-speaking input that is already in its target language: feeding the mic to both
   sessions produced 64–402 spurious characters per run (the patient→English session
   repeating the doctor's English). It also produced the language misdetection seen in
   `both_conv4_g30` (Mandarin heard as Vietnamese). **Option A (route the stream) is used.**
2. **Continuous input is at least as good as gated.** Conversation set, 5 runs each:
   Mandarin recall 0.950–0.955 (continuous) vs 0.939 (gated); final-clause drops 0/50
   (`routed_nothing`) vs 6/50 (gated). Latency is equal (first audio ~2.5 s, last
   output ~1.3 s after speech ends).
3. **`audio_stream_end` on release is adopted.** It is the auto-VAD way to tell the
   model "the mic is off"; with it the first translated audio arrives ~1 s sooner
   (1.0–1.6 s vs ~2.5 s) and drops stay low (3/30).
4. **Idle session gets nothing.** `nothing` ≥ `noise` ≥ `zero` on drops; no session
   closed or went cold even at 30 s gaps (`closes` = 0 everywhere).
5. **Pre-roll is required.** After a long idle gap the first syllable is clipped
   (`我咳嗽…` heard as `咳嗽…`/`所以…`, g30 rows). A single-session check
   (`starter_repro.py --lead`) shows 300 ms of audio before speech restores it in every
   run, so the server keeps the last 300 ms of unrouted mic audio and sends it when the
   route opens. Verified live: `tests/integration/ws_live.py --clips conv` 10/10.
6. **Feedback:** naive suppression (`sup`) is wrong — it replaces the *next speaker's*
   mic while the previous translation still plays, destroying their speech
   (recall 0.0–0.14). The app instead stops uploading only while translation is
   playing **and nobody holds the mic** (turn mode), which is exactly when nobody can
   be speaking; in live mode it relies on browser echo cancellation.
7. **The `"digital silence makes the model drop the tail"` claim does not hold** for
   continuous input: `routed_zero` (digital-zero idle and floor) is in line with the
   other routed conditions.

## Known model behaviour (not pipeline defects)

- `turn-p4` "这药" is heard as "这要/这样" whenever the clip reaches a session without
  prior patient context — in isolation it is misheard in 6/6 runs under **both** auto
  and manual VAD and every lead-in length. Gated runs got it right from earlier-turn
  context. Homophone on a TTS clip; the translation stays close in meaning.
- Spoken Chinese decimals come back as `38度5`, `38.5`, `3 8度5` or occasionally a
  dropped `.5`; the app's number check exists for exactly this.
- Numbers can be omitted by the model (`suite-a` 06 once dropped 三次); the app flags it.

## Longevity (Phase 4)

`tests/e2e/longevity.mjs`: 60 turns, ~25 min, one consult, **no context
compression** — 60/60 turns completed, every turn handed over ("Your turn"), no
errors; reconnects (if any) were absorbed by handle resumption. Compression stays
removed.

## Deviations from the plan (deliberate)

- **Turns still exist in the UI.** Option A keeps explicit TALK taps, so a
  transcript line is still one tap (not `LINE_GAP_MS` segmentation, plan 3.5), and
  the playback-side handover heuristic (`startDrain`/`closeTurn`, `QUIESCE_MS`,
  `COLD_GRACE_MS` = 9000 as before, caps) is kept: it decides when the buttons
  re-enable. None of it touches what the model hears — the input side has no
  turn markers, trimming, padding or warm-up.
- **Feedback guard is narrower than plan 3.4.** Naive suppression destroyed the
  next speaker's speech in the matrix; the app instead pauses upload only while
  translation plays *and* nobody holds the mic, and the server ages out (0.45 s)
  and clears (on release) its pre-roll.
- **Phase 5 is implemented but not exercised live.** `GEMINI_BACKEND=enterprise`
  builds the starter's `genai.Client(enterprise=True, ...)` (verified to construct
  a Vertex client) and turns on transparent resumption; there are no Google Cloud
  credentials on the test machine, so no live enterprise run exists yet. Run
  `uv run experiments/multiturn.py --backend enterprise ...` once ADC is set up.
