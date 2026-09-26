# Factory feedback — consumer cycle 1

**Consumer repo:** `marcusjhang/live-translate-diarize-poc` (private)
**Work item:** `W-7fef699d` — *"Build a SIMPLE, turn-based, two-person live interpretation app…"*
**Run:** `psf run --git --harness opencode --model deepseek/deepseek-v4-pro --mode yolo --no-ask`
**Factory:** psf `0.1.0`, digest `sha256:f21bac89b0ddbd4317a07321618f3adca116fa6955f29a604803a67bcd4ea44b`
**Envelope:** `FB-07f94279` → filed upstream as issue #2
**Follow-up issues:** #7 (`verify_command` needs `--git`), #8 (feedback export fails silently)

---

## 1. Headline

**Every gate was green and the artifact could not run.**

The foreman took the goal through triage → spec → approval → build → *two independent
verifications* → review, and reported:

- `verify_command` (`uv run server.py --self-check`) — passed
- verification quorum of 2 — passed
- review — `approve`, "all acceptance criteria met"
- handoff produced

The delivered app **could not open a session with the translation API at all**. Not
"degraded", not "missing a feature" — a hard exception on the first connection, so it was
non-functional as a product. It took **8 high-severity defects** to make it work, several of
which are only observable at runtime.

The recorded outcome is therefore `accepted = true` **and** `review_escape = true`.

---

## 2. Why the gates could not see it

Three structural reasons, in order of impact. These are the real feedback.

### 2.1 `verify_command` does not touch the dependency that breaks

The factory's deterministic gate ran a **keyless self-check of pure functions**. The isolated
git worktree contains no `.env`, so the verifier agents cannot reach the API either. Every
gate therefore inspects *shape*, and none inspects *behaviour against the real dependency*.

The single most damaging defect — a config field that is valid on one auth path and raises on
the other — is invisible to any amount of code reading. It is only visible by connecting.

> An integration can pass every gate the factory has while being unable to talk to its
> dependency. Right now the factory has no way to notice.

### 2.2 Acceptance criteria were satisfiable by *plausible-looking* code

The criteria were prose ("the page shows…", "the turn closes when…"). Nothing required the
verifier to produce **evidence from a run**. So verification reduced to "does the code appear
to implement the described thing", which passes for code that cannot execute.

One criterion was worse: it asked for a turn state machine, the implementer shipped a state
machine, the self-check asserted it — and the server **never called it**. The machine was dead
code that existed only to make the gate look meaningful. Verification passed on logic that
was not in the execution path.

### 2.3 Nothing exercised the *second* run

Several defects only appear after the first use:

- The opening turn captured **silence** every session (an audio context created outside a user
  gesture starts suspended and was only resumed on the *next* turn).
- The language pair was persisted and the setup screen then **skipped on every later load** —
  so the settings were permanent and unchangeable, and a shared device handed the second
  patient the first patient's languages.
- A pending timer **erased the error state**, so a connection failure silently returned to
  "ready".
- The **Retry button did not work** — it re-entered the error state it was meant to clear.

Every one of these is a lifecycle bug. A single-run, single-session check cannot find them.

---

## 3. The defects, generalised into gate gaps

Eight high-severity defects reached handoff. Grouped by the gate that should have caught them:

| # | Defect (class) | Observable only at… | Gate that missed it |
|---|---|---|---|
| 1 | Auth-path-specific config rejected by the SDK | connect time | `verify_command` never connected |
| 2 | Turn never closed: the completion signal the design assumed **never fires** | runtime, per turn | spec asserted an API behaviour that was never verified against the API |
| 3 | Late streaming output misattributed to the *following* turn, producing transcript content nobody spoke | multi-turn runtime | no runtime evidence required |
| 4 | A benign session close treated as fatal, killing the direction permanently | runtime, logs | no log/health check in verification |
| 5 | Silence/activity detector used the wrong statistic; the signal it measured was dominated by noise | runtime | "buffers audio" is satisfied by code that buffers nothing |
| 6 | Required trailing silence sent as an instant burst instead of paced — silently truncated the final clause | runtime, content | content correctness not in acceptance |
| 7 | Capture context created outside a user gesture → first turn silent | runtime, first turn | no second-run check |
| 8 | Persisted settings skipped the setup screen → permanent, unchangeable config | second load | no lifecycle/restart criterion |

Notice the pattern: **every one is a runtime or lifecycle property, and the gate suite is
entirely static.**

---

## 4. Recommendations

Ordered by expected value. Each is written so the factory can hold itself to it.

**R1 — Make the gate prove it can bite.** Before trusting `verify_command`, run it against the
**base commit** (pre-build). If it also passes there, it is not verifying this change. A gate
that passes on the baseline is a gate that would pass on an empty diff. This one check would
have caught the vacuous self-check immediately.

**R2 — Require runtime evidence per acceptance criterion.** Each criterion should resolve to
either (a) a command whose output is captured and attached to the verification record, or
(b) an explicit declaration that it is unverifiable in the sandbox, which then *blocks* rather
than silently passing. "The verifier read the code and believes it" is not verification for a
criterion that describes behaviour.

**R3 — Detect when `verify_command` cannot reach the changed surface.** If the change touches
an external integration and the verify command makes no network calls and consumes no
credentials, say so. Warn or downgrade the verification's confidence. A keyless self-check
must not count as verification of an integration.

**R4 — Flag logic that is only exercised by tests.** Any module-level function or class
reachable only from the self-check/tests and never from the runtime path is dead. This is a
cheap static check and it would have exposed the dead turn machine — and with it, the
vacuous gate.

**R5 — Add a lifecycle verification pass.** For work that produces a service or UI, the
verification must include: second run, reload/restart, reconnect after failure, and recovery
from an error state. Most of the list in §3 is only reachable here.

**R6 — Ban `assert` in verify commands.** `python -O` strips them, turning the gate into a
silent no-op that still exits 0. Require explicit failure (`raise SystemExit(1)` or an
equivalent) so the gate cannot be neutered by an interpreter flag.

**R7 — Require the spec to state which API behaviours were *assumed* vs *observed*.** Defect 2
came from a plan that assumed a completion event exists. The plan was confident; the event
never fires. Asking the spec step to label assumptions makes them verifiable and stops the
verifier from treating them as given.

**R8 — Make the review gate able to block on "it does not run".** Review approved code that
could not start. Review is explicitly advisory in the factory, which is fine for style, but
"the artifact cannot execute" should be a blocking condition regardless.

---

## 5. A bug in the feedback loop itself

`psf feedback export --github <upstream>` **fails silently and files nothing** on a repository
that lacks the `factory-feedback` label:

```
wrote .psf/feedback/FB-c96d13cb.json
issue create failed: could not add label: 'factory-feedback' not found
```

No non-zero exit, no retry, and the local envelope looks written — so a consumer would believe
feedback was sent when it was dropped. I created the missing label upstream and the export then
filed correctly.

Suggested fix:

- create `factory-feedback` as part of the factory's own setup, and
- make the export **verify the created issue URL** and exit non-zero with a clear message when
  filing fails.

Filed upstream as **issue #8**.

---

## 6. A second factory defect: `verify_command` vs the temp-dir workspace

Found while smoke-testing a clean clone: `psf run "<goal>"` **cannot reach `DONE`** in this
repo unless `--git` is passed, and it reports an error that looks like a code defect.

```
psf run "<goal>"          ->  BLOCKED   attempts 3/3
psf run "<goal>" --git    ->  DONE      attempts 1/3

verify_command failed: error: Failed to spawn: `server.py`
  Caused by: No such file or directory (os error 2)
```

`Workspace.create()` with `use_git=False` returns `tempfile.mkdtemp(...)` — an empty
directory. A `verify_command` that references a project file can therefore only fail, so the
item blocks deterministically no matter how good the agents are, and `max_attempts` cannot
help. The README quickstart is the invocation that fails.

This matters beyond one consumer: `verify_command` referencing a project file is the natural
way to write one, and the failure mode is silent about its cause. Filed upstream as **issue #7**.

Together with §5, both defects are in the factory's own delivery and verification paths, not in
any particular consumer's usage.

## 7. Suggested eval cases

Concrete additions, in the factory's own idiom:

1. **`gate-must-bite`** — apply a known-bad change; assert `verify_command` fails. Reject any
   factory whose verify command passes on the baseline.
2. **`no-dead-logic-under-test`** — a change whose tested function is unreachable from the
   runtime must be flagged (or must fail verification).
3. **`second-run`** — a scripted second invocation of the built artifact must succeed and must
   not inherit state from the first.
4. **`failure-is-loud`** — induce the dependency to fail and assert the artifact reports it,
   rather than appearing healthy.
5. **`recovery`** — induce a failure, restore the dependency, and assert recovery works (this
   is what exposed the broken Retry button).
6. **`assert-free-gate`** — run the verify command under `python -O`; it must still fail when
   it should.
7. **`envelope-arrives`** — assert `feedback export --github` produces a reachable issue URL.

---

## 8. What the factory got right

Worth keeping, because the failure above is narrow rather than general:

- **The process was genuinely followed.** Spec, approval, build, two independent
  verifications, review, handoff — all recorded in the ledger, chain intact, replayable.
- **The seam between build agents and the controller held.** No agent wrote lifecycle state.
- **The `--git` isolation worked.** The handoff was a real worktree on a real branch, and the
  diff was clean.
- **The spec was good.** Its acceptance criteria were specific and mostly correct; the problem
  was that nothing forced them to be *executed*.
- **The ledger made this analysis possible.** Tracing exactly which gate passed what is why I
  can write a precise report instead of a vague complaint.

The factory's weakness is not discipline or structure. It is that **its notion of "verified"
stops at the boundary of the sandbox**, exactly where real integrations begin.

---

## 9. Privacy

This document describes failure *classes*, gate behaviour, and process. It contains no
consumer source code, no prompts, no file paths, no credentials, and no customer data. The
machine envelope filed as issue #2 contains counts, digests and versions only.
