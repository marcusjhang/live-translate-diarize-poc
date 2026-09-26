#!/usr/bin/env python3
"""Generate the speech fixtures for the e2e suites.

macOS only: uses `say` for TTS and `afconvert` to produce 16 kHz mono PCM16 WAV,
which is what the app streams to the model. Run once before the suites:

    python3 tests/e2e/gen-clips.py

Every fixture is deterministic text, so this reproduces the exact corpus the
suites were written against. No microphone is involved anywhere in these tests.
"""
import os
import subprocess
import sys
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
CLIPS = HERE / "clips"

EN, ZH, ES = "Samantha", "Tingting", "Monica"

# name -> (text, voice, pre_silence_ms, internal_pause)
# internal_pause is a tuple of (first_text, second_text, gap_ms) for the
# thinking-pause fixture.
CLIP_SPECS = {
    # --- suite A -----------------------------------------------------------
    "s01-d1": ("Show me where it hurts.", EN, 0, None),
    "s02-p1": ("我这里一直疼。", ZH, 0, None),
    "s04-d1": ("Tell me about the pain, when it started, what makes it worse, "
               "and whether it spreads anywhere else.", EN, 0, None),
    "s05-d1": ("Any pain?", EN, 0, None),
    "s06-d1": ("How long has this been going on?", EN, 0, None),
    "s06-d2": ("And does it hurt at night?", EN, 0, None),
    "s07-d1": ("Take 500 milligrams three times a day for seven days.", EN, 0, None),
    "s08-p1": ("我的体温是38度5。", ZH, 0, None),
    "s09-d1": ("Do not take this on an empty stomach.", EN, 0, None),
    "s10-d1": ("Does it hurt when you breathe?", EN, 0, None),
    "s11-d1": (None, EN, 0, ("I want to ask you", "about your sleep.", 2000)),
    "s12-d1": ("I am going to examine you now.", EN, 0, None),
    "s13-d1": ("Where does it hurt?", EN, 0, None),
    "s13-p1": ("Me duele la cabeza.", ES, 0, None),
    "s14-d1": ("Do you have a fever?", EN, 0, None),
    "s15-d1": ("Hello there.", EN, 0, None),
    # --- suite B -----------------------------------------------------------
    "c-d1":   ("Do you have any allergies?", EN, 0, None),
    "c-p1":   ("我没有过敏。", ZH, 0, None),
    "c-d2":   ("Good, thank you.", EN, 0, None),
    "c-p2":   ("谢谢医生。", ZH, 0, None),
    "b03-d1": ("I am going to ask you three things.", EN, 0, None),
    "b03-d2": ("First, how is your appetite?", EN, 0, None),
    "b03-d3": ("Second, are you sleeping well?", EN, 0, None),
    "b04-d1": ("Take 500 milligrams with 10 millilitres of water.", EN, 0, None),
    "b05-d1": ("Take it every eight hours for three days.", EN, 0, None),
    "b06-d1": ("Do not take more than two tablets in one day.", EN, 0, None),
    "b07-d1": ("Tell me if you have fever, cough, or a rash.", EN, 0, None),
    "b08-d1": ("Your blood pressure is high. We will check it again next week.", EN, 0, None),
    "b09-p1": ("How many times a day should I take it?", EN, 0, None),
    "b10-p1": ("I am worried about the side effects.", EN, 0, None),
    "b11-d1": ("You should come back if the pain gets worse.", EN, 0, None),
    "b12-d1": ("Please take this medicine after food.", EN, 0, None),
    "b13-d1": ("Tell me about the pain, when it started, what makes it worse, "
               "whether it spreads anywhere else, and how it affects your sleep at night.",
               EN, 0, None),
    "b14-p1": ("我头疼，还有一点恶心。", ZH, 8000, None),
    # --- conversation / longevity ------------------------------------------
    "turn-d1": ("Good morning, Mrs. Chen. Tell me what has been going on.", EN, 0, None),
    "turn-d2": ("How many days have you had this cough and fever?", EN, 0, None),
    "turn-d3": ("Take one tablet twice a day for five days.", EN, 0, None),
    "turn-d4": ("Do not skip any doses, even if you start feeling better.", EN, 0, None),
    "turn-d5": ("Come back in one week if the fever continues.", EN, 0, None),
    "turn-p1": ("我咳嗽发烧已经五天了。", ZH, 0, None),
    "turn-p2": ("昨天量体温有三十八度五。", ZH, 0, None),
    "turn-p3": ("这个药一天要吃几次？", ZH, 0, None),
    "turn-p4": ("我怕这药吃了会犯困。", ZH, 0, None),
    "turn-p5": ("谢谢医生，麻烦您了。", ZH, 0, None),
}


def synth(text, voice, path, pause_after_ms=0):
    aiff = path.with_suffix(".aiff")
    subprocess.run(["say", "-v", voice, "-o", str(aiff), text], check=True)
    subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1",
                    str(aiff), str(path)], check=True)
    aiff.unlink(missing_ok=True)
    with wave.open(str(path)) as w:
        pcm = w.readframes(w.getnframes())
    if pause_after_ms:
        pcm += b"\x00\x00" * int(16000 * pause_after_ms / 1000)
    return pcm


def main():
    if sys.platform != "darwin":
        sys.exit("gen-clips.py uses macOS `say`/`afconvert`; not available here.")
    CLIPS.mkdir(parents=True, exist_ok=True)
    for name, (text, voice, pre_ms, internal) in CLIP_SPECS.items():
        path = CLIPS / f"{name}.wav"
        if internal:
            a, b, gap = internal
            pcm = synth(a, voice, path, pause_after_ms=gap)
            b_pcm = synth(b, voice, path.with_suffix(".tmp.wav"))
            pcm += b_pcm
            path.with_suffix(".tmp.wav").unlink(missing_ok=True)
        else:
            pcm = synth(text, voice, path)
        if pre_ms:
            pcm = b"\x00\x00" * int(16000 * pre_ms / 1000) + pcm
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
            w.writeframes(pcm)
        with wave.open(str(path)) as w:
            dur = w.getnframes() / w.getframerate()
        print(f"  {name:10} {dur:5.2f}s")
    print(f"\n{len(CLIP_SPECS)} fixtures -> {CLIPS}")


if __name__ == "__main__":
    main()
