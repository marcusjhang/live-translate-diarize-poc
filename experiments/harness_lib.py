"""Pure logic for the multi-turn Live Translate harness (experiments/multiturn.py).

No SDK, no network: everything here is unit-tested in tests/test_harness_lib.py.
Audio is 16-bit little-endian mono PCM as `bytes`; sample arrays are `array('h')`.
"""
from __future__ import annotations

import importlib.util
import math
import random
import re
import sys
from array import array
from dataclasses import dataclass, field
from pathlib import Path

IN_RATE = 16000
OUT_RATE = 24000
CHUNK_SAMPLES = IN_RATE // 10  # 100 ms
CHUNK_BYTES = CHUNK_SAMPLES * 2

ROOT = Path(__file__).resolve().parent.parent
CLIPS_DIR = ROOT / "tests" / "e2e" / "clips"

# --- clip catalogue -----------------------------------------------------------

_VOICE_LANG = {"Samantha": "en", "Tingting": "zh", "Monica": "es"}

# The model transcribes spoken Chinese numbers as digits ("三十八度五" -> "38.5度"),
# so score those clips against the digit form or every run "drops" the tail.
SCORING_TEXT = {
    "turn-p2": "昨天量体温有38.5度。",
    "s08-p1": "我的体温是38.5度。",
}


def load_clip_specs() -> dict[str, tuple[str, str]]:
    """name -> (expected_text, lang) from tests/e2e/gen-clips.py (single source)."""
    path = ROOT / "tests" / "e2e" / "gen-clips.py"
    spec = importlib.util.spec_from_file_location("gen_clips", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("gen_clips", mod)
    spec.loader.exec_module(mod)
    out = {}
    for name, (text, voice, _pre, internal) in mod.CLIP_SPECS.items():
        if text is None and internal:
            text = f"{internal[0]} {internal[1]}"
        out[name] = (SCORING_TEXT.get(name, text), _VOICE_LANG[voice])
    return out


# Output-side tail check: the translation of each clip's LAST clause must contain
# one of these (case-insensitive). This is what a listener loses when the tail drops.
OUT_TAIL = {
    "turn-d1": ["发生", "怎么了", "情况"],
    "turn-p1": ["five days", "5 days"],
    "turn-d2": ["发烧", "发热"],
    "turn-p2": ["38.5", "thirty-eight point five"],
    "turn-d3": ["五天", "5天", "5 天"],
    "turn-p3": ["a day", "per day", "daily"],
    "turn-d4": ["好"],
    "turn-p4": ["sleepy", "drowsy", "tired"],
    "turn-d5": ["回来", "复诊", "再来", "回诊"],
    "turn-p5": ["bother", "trouble", "appreciate", "grateful"],
    "s11-d1": ["睡"],
    "c-p1": ["allerg"],
    "s07-d1": ["七天", "7天", "7 天"],
    "s08-p1": ["38.5"],
    "b13-d1": ["睡", "晚上", "夜"],
}


def out_tail_ok(clip: str, said: str) -> bool | None:
    keys = OUT_TAIL.get(clip)
    if keys is None:
        return None
    s = said.lower()
    return any(k.lower() in s for k in keys)


def speaker_of(name: str) -> str:
    """Clip naming convention: '-dN' is the doctor, '-pN' the patient."""
    m = re.search(r"-([dp])\d+$", name)
    if not m:
        raise ValueError(f"cannot tell speaker of clip {name!r}")
    return "doctor" if m.group(1) == "d" else "patient"


# --- schedule -----------------------------------------------------------------

@dataclass
class Segment:
    name: str
    speaker: str
    lang: str
    text: str
    start_chunk: int          # first 100 ms tick of the clip
    n_chunks: int             # clip length in ticks (rounded up)
    pcm: bytes = field(repr=False, default=b"")

    @property
    def end_chunk(self) -> int:
        return self.start_chunk + self.n_chunks


def build_schedule(clips: list[tuple[str, str, str, bytes]], gap_s: float,
                   lead_in_s: float = 1.0) -> list[Segment]:
    """clips: (name, lang, text, pcm). Returns segments laid on a 100 ms tick grid,
    `gap_s` of no-speech between consecutive clips, after `lead_in_s`."""
    t = round(lead_in_s * 10)
    gap = round(gap_s * 10)
    segs = []
    for name, lang, text, pcm in clips:
        n = math.ceil(len(pcm) / CHUNK_BYTES)
        segs.append(Segment(name, speaker_of(name), lang, text, t, n, pcm))
        t += n + gap
    return segs


def clip_chunk(seg: Segment, tick: int) -> bytes:
    """The 100 ms of `seg` that plays at `tick` (zero-padded on the last chunk)."""
    i = (tick - seg.start_chunk) * CHUNK_BYTES
    c = seg.pcm[i:i + CHUNK_BYTES]
    return c + b"\x00" * (CHUNK_BYTES - len(c))


def segment_at(segs: list[Segment], tick: int) -> Segment | None:
    for s in segs:
        if s.start_chunk <= tick < s.end_chunk:
            return s
    return None


# --- signal helpers -----------------------------------------------------------

def noise_chunk(rng: random.Random, rms: float) -> bytes:
    """100 ms of Gaussian noise at the given RMS (int16 units). Models a real mic's
    noise floor; rms=0 gives digital silence."""
    if rms <= 0:
        return b"\x00" * CHUNK_BYTES
    a = array("h", (max(-32768, min(32767, int(rng.gauss(0, rms)))) for _ in range(CHUNK_SAMPLES)))
    return a.tobytes()


def mix(a: bytes, b: bytes, gain_b: float = 1.0) -> bytes:
    """a + gain_b*b, clipped. Shorter input is zero-extended."""
    x, y = array("h"), array("h")
    x.frombytes(a)
    y.frombytes(b)
    n = max(len(x), len(y))
    out = array("h", bytes(2 * n))
    for i in range(n):
        v = (x[i] if i < len(x) else 0) + gain_b * (y[i] if i < len(y) else 0)
        out[i] = max(-32768, min(32767, int(v)))
    return out.tobytes()


def rms(pcm: bytes) -> float:
    a = array("h")
    a.frombytes(pcm[: len(pcm) // 2 * 2])
    if not a:
        return 0.0
    return math.sqrt(sum(v * v for v in a) / len(a))


def resample_24k_to_16k(pcm24: bytes) -> bytes:
    """Linear-interpolation 3:2 downsample, for feeding speaker output back to the mic."""
    a = array("h")
    a.frombytes(pcm24[: len(pcm24) // 2 * 2])
    n_out = len(a) * 2 // 3
    out = array("h", bytes(2 * n_out))
    for j in range(n_out):
        pos = j * 1.5
        i = int(pos)
        f = pos - i
        v0 = a[i]
        v1 = a[i + 1] if i + 1 < len(a) else v0
        out[j] = int(v0 + (v1 - v0) * f)
    return out.tobytes()


# --- routing ------------------------------------------------------------------

def route(mode: str, active_speaker: str | None) -> set[str]:
    """Which sessions receive the live mic this tick.
    mode 'routed' (option A): only the active speaker's session (the mic is routed by
    the button; with nobody speaking it stays on the last speaker — callers pass that).
    mode 'both' (option B): both sessions always."""
    if mode == "both":
        return {"doctor", "patient"}
    if mode == "routed":
        return {active_speaker} if active_speaker else set()
    raise ValueError(mode)


# --- scoring ------------------------------------------------------------------

_TRAD = dict(zip("沒過體溫這藥會個麼謝醫幾時說頭還們發燒", "没过体温这药会个么谢医几时说头还们发烧"))
_PUNCT = re.compile(r"[\s\.,!?;:，。！？；：、\"'“”‘’()（）-]+")


def tokens(text: str, lang: str) -> list[str]:
    """Words for space-delimited languages, characters for Chinese. Digits are kept
    as whole numbers in both (so '38.5' and '500' are single tokens)."""
    text = text.lower()
    if lang == "zh":
        # The input transcript drifts into Traditional characters and spaced digits
        # ("3 8度5"); neither is a hearing error.
        text = "".join(_TRAD.get(c, c) for c in text)
        text = re.sub(r"(?<=\d) (?=\d)", "", text)
        out = []
        for m in re.finditer(r"\d+(?:\.\d+)?|[^\W\d_]", text):
            out.append(m.group(0))
        return out
    return [t for t in _PUNCT.split(text) if t]


def recall(expected: str, got: str, lang: str) -> float:
    """Share of expected tokens found in `got` (multiset, order-insensitive)."""
    exp = tokens(expected, lang)
    if not exp:
        return 1.0
    pool = {}
    for t in tokens(got, lang):
        pool[t] = pool.get(t, 0) + 1
    hit = 0
    for t in exp:
        if pool.get(t, 0) > 0:
            pool[t] -= 1
            hit += 1
    return hit / len(exp)


def tail_heard(expected: str, got: str, lang: str, k: int = 2) -> bool:
    """Did the model hear the last k expected tokens? (dropped-final-clause check)"""
    exp = tokens(expected, lang)[-k:]
    g = set(tokens(got, lang))
    return all(t in g for t in exp)


@dataclass
class Event:
    t: float          # seconds since schedule start
    session: str      # 'doctor' | 'patient'
    kind: str         # 'in' | 'out' | 'audio' | 'turn_complete' | 'go_away' | 'closed' | 'reconnect'
    text: str = ""
    loud: bool = False


def attribute(events: list[Event], segs: list[Segment], tail_s: float = 6.0) -> dict[int, list[Event]]:
    """Assign each session's in/out/audio event to the most recent segment *of that
    session's speaker* that started before it, if within that segment's end + tail_s.
    Events matching no own-speaker segment are spurious (key -1)."""
    out: dict[int, list[Event]] = {-1: []}
    for i in range(len(segs)):
        out[i] = []
    for e in events:
        if e.kind not in ("in", "out", "audio"):
            continue
        best = None
        for i, s in enumerate(segs):
            if s.speaker != e.session:
                continue
            if s.start_chunk / 10 <= e.t <= s.end_chunk / 10 + tail_s:
                best = i
        out[best if best is not None else -1].append(e)
    return out


def score(events: list[Event], segs: list[Segment], tail_s: float = 6.0) -> dict:
    """Per-segment and aggregate metrics for one run."""
    by = attribute(events, segs, tail_s)
    rows = []
    for i, s in enumerate(segs):
        ev = by[i]
        heard = "".join(e.text for e in ev if e.kind == "in")
        said = "".join(e.text for e in ev if e.kind == "out")
        loud = [e for e in ev if e.kind == "audio" and e.loud]
        end_t = s.end_chunk / 10
        first_audio = min((e.t for e in loud), default=None)
        last_out = max((e.t for e in ev if e.kind == "out"), default=None)
        nxt = segs[i + 1].start_chunk / 10 if i + 1 < len(segs) else None
        late = [e for e in ev if e.kind == "out" and nxt is not None and e.t > nxt]
        rows.append({
            "clip": s.name, "speaker": s.speaker,
            "input_recall": round(recall(s.text, heard, s.lang), 3),
            "tail_heard": tail_heard(s.text, heard, s.lang),
            "out_chars": len(said),
            "empty": len(said) == 0,
            "out_tail_ok": out_tail_ok(s.name, said),
            "first_audio_s": None if first_audio is None else round(first_audio - s.start_chunk / 10, 2),
            "last_out_after_end_s": None if last_out is None else round(last_out - end_t, 2),
            "late_out_chars": sum(len(e.text) for e in late),
            "heard": heard, "said": said,
        })
    spurious = [e for e in by[-1] if e.kind == "out"]
    agg = {
        "clips": len(rows),
        "mean_input_recall": round(sum(r["input_recall"] for r in rows) / max(1, len(rows)), 3),
        "tails_dropped": sum(not r["tail_heard"] for r in rows),
        "empty_outputs": sum(r["empty"] for r in rows),
        "out_tails_dropped": sum(r["out_tail_ok"] is False for r in rows),
        "late_out_chars": sum(r["late_out_chars"] for r in rows),
        "spurious_out_chars": sum(len(e.text) for e in spurious),
        "spurious_text": "".join(f"[{e.session}]{e.text}" for e in spurious)[:300],
        "median_first_audio_s": _median([r["first_audio_s"] for r in rows if r["first_audio_s"] is not None]),
        "median_last_out_after_end_s": _median([r["last_out_after_end_s"] for r in rows
                                                if r["last_out_after_end_s"] is not None]),
        "closes": sum(1 for e in events if e.kind in ("closed", "go_away")),
        "turn_completes": sum(1 for e in events if e.kind == "turn_complete"),
    }
    return {"rows": rows, "agg": agg}


def _median(xs):
    xs = sorted(xs)
    if not xs:
        return None
    m = len(xs) // 2
    return round(xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2, 2)
