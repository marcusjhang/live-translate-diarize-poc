# End-to-end tests

These drive the **real app against the real Gemini Live Translate API** through a real
browser, and assert on what appears in the DOM. They are not unit tests and they are not
mocked: every turn sends actual audio through the app's own capture → encode → WebSocket
path and waits for the model.

The one thing they do **not** use is a microphone. Speech fixtures are pre-generated WAVs
played into a synthetic `MediaStream`, so the tests are deterministic and need no quiet room.
See [Limitations](#limitations) for what that leaves untested.

---

## Prerequisites

| | |
|---|---|
| Node | ≥ 20 (uses the global `WebSocket` and top-level `await`) |
| Server | `uv run server.py` running with `GEMINI_API_KEY` in `.env` |
| Browser | a Superset browser pane, for its Chrome DevTools Protocol endpoint |
| Optional | macOS, only to *regenerate* fixtures (`say` + `afconvert`) |

### Get the CDP endpoint

The endpoint URL embeds an auth token, so it is never committed. Write it to a file and
point `CDP_FILE` at it (default `/tmp/cdp.txt`):

```bash
superset browser open --workspace "$SUPERSET_WORKSPACE_ID" \
  --url http://localhost:8000 --target new-tab --json      # note the paneId
superset browser cdp --workspace "$SUPERSET_WORKSPACE_ID" --pane <paneId> --json \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["url"])' > /tmp/cdp.txt
```

The suites clear `localStorage`, set the language pair, tick the consent box and click
Start themselves — you do not need to prepare the page.

---

## Running

```bash
node tests/e2e/number-integrity.mjs   # no server, no browser, no key needed
node tests/e2e/suite-a.mjs            # 15 scenarios: single turns, numbers, languages
node tests/e2e/suite-b.mjs            # 15 scenarios: multi-turn, units, negations, en→fil
node tests/e2e/misuse.mjs             # interrupt, simultaneous press, press during playback
node tests/e2e/disconnect.mjs e3      # press during playback, probed mid-turn
node tests/e2e/conversation.mjs       # one 10-turn consultation
node tests/e2e/longevity.mjs          # 60 turns (~12 min); crosses the 10-minute cap
node tests/e2e/check-consent.mjs      # consent gate + language defaults
node tests/e2e/check-interpreter.mjs  # "Call interpreter" overlay
```

`disconnect.mjs e5` needs the server killed while it runs — start it, then
`sleep 10 && pkill -f "uv run server.py"` in another shell.

Results print as a PASS/FAIL table and are also written to `/tmp/e2e-*-results.json`.
Every suite exits 0 only if all its scenarios passed.

---

## Fixtures

`clips/` holds 44 committed WAVs (16 kHz mono PCM16, ~3.3 MB) so the suites run anywhere.
They are derived, not source: regenerate them on macOS with

```bash
python3 tests/e2e/gen-clips.py
```

Every fixture is a fixed string in that script, so the corpus is reproducible and diffable.

---

## What is covered

- **Turn mechanics** — single turn, very short, very long (26 words), multi-sentence,
  same speaker 2× and 3×, 4- and 8-turn alternation, 10-turn consult, 60-turn longevity.
- **Content integrity** — dosages (`500 mg three times a day for seven days`), clinical units
  (`500 milligrams with 10 millilitres`), intervals and durations (`every eight hours for
  three days`), decimals in both notations (`38.5` / `38度5` / `38度五`), negations
  (`do not take more than two tablets`), enumerations, questions, emotional language.
- **Languages** — `en↔zh`, `en→es`, `es→en`, `en→fil`, and same-language-both-sides.
- **Misuse** — interrupting mid-turn, both buttons in the same tick, pressing during
  playback, a silent double-tap, and a mid-turn disconnect.
- **Lifecycle** — the error state survives; Retry recovers; the consent gate holds;
  languages can be changed between patients; playback is silenced so the room stays quiet.

Verified with these: **30/30 scenarios**, 20/20 unit cases, 5/5 misuse cases, 0 server
tracebacks, and a 60-turn session that survives the 10-minute Live API cap and reconnects.

---

## Limitations

Read these before trusting a green run.

**No microphone, ever.** A real device has only been confirmed to *open* — enumerated,
permission granted, no error. No human has spoken into this app. Accents, quiet elderly
speech, mumbling, and clinic background noise are entirely untested. Echo cancellation is
enabled and correctly cancels speaker output, so an acoustic loopback test cannot work.

**The model is not deterministic.** The same scenario passes and fails across runs —
`es↔en` has produced fluent output on one run and gibberish (`Me dole lá,`) on another.
A green run is evidence, not a guarantee. Treat this as a smoke suite, not a regression gate.

**Known-flaky scenarios** — `A13` (`es→en`) and occasionally `A09`/`A15`, where the model
returns no translation. Both are surfaced by the app's own flags when they happen.

**Model-side failures are detected, not fixed.** The app flags a dropped number, an
untranslated turn, and collapsed output. One failure is deterministic and cannot be fixed
here: `"Take one tablet twice a day for five days"` loses *"twice a day"* in 6 of 6
attempts. `README.md` documents the measured frequencies.

**Not covered at all** — the physical tablet layout and viewing angles; speaker volume and
audibility; more than one session at a time; language pairs other than those listed;
sessions longer than ~12 minutes.
