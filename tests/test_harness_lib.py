"""Unit tests for the pure logic behind experiments/multiturn.py."""
import random
import sys
import unittest
from array import array
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "experiments"))

import harness_lib as H  # noqa: E402


def pcm(n_samples, value=1000):
    return array("h", [value] * n_samples).tobytes()


class ClipCatalogue(unittest.TestCase):
    def test_specs_load_with_language(self):
        specs = H.load_clip_specs()
        self.assertEqual(specs["s01-d1"], ("Show me where it hurts.", "en"))
        self.assertEqual(specs["c-p1"][1], "zh")
        self.assertIn("about your sleep", specs["s11-d1"][0])  # internal-pause clip joined
        self.assertIn("38.5", specs["turn-p2"][0])  # scored against the digit form

    def test_speaker_of(self):
        self.assertEqual(H.speaker_of("turn-d3"), "doctor")
        self.assertEqual(H.speaker_of("c-p1"), "patient")
        with self.assertRaises(ValueError):
            H.speaker_of("hello")


class Schedule(unittest.TestCase):
    def test_layout_on_100ms_grid(self):
        clips = [("a-d1", "en", "x", pcm(16000)), ("b-p1", "zh", "y", pcm(1601))]
        segs = H.build_schedule(clips, gap_s=2.0, lead_in_s=1.0)
        self.assertEqual((segs[0].start_chunk, segs[0].n_chunks), (10, 10))
        self.assertEqual((segs[1].start_chunk, segs[1].n_chunks), (40, 2))  # 1601 samples -> 2 chunks
        self.assertIs(H.segment_at(segs, 9), None)
        self.assertIs(H.segment_at(segs, 10), segs[0])
        self.assertIs(H.segment_at(segs, 20), None)
        self.assertIs(H.segment_at(segs, 41), segs[1])

    def test_clip_chunk_is_exactly_100ms_and_zero_padded(self):
        seg = H.build_schedule([("a-d1", "en", "x", pcm(1700))], 0, 0)[0]
        self.assertEqual(len(H.clip_chunk(seg, 0)), H.CHUNK_BYTES)
        last = H.clip_chunk(seg, 1)
        self.assertEqual(len(last), H.CHUNK_BYTES)
        self.assertEqual(last[200:], b"\0" * (H.CHUNK_BYTES - 200))


class Signal(unittest.TestCase):
    def test_noise_floor_rms(self):
        c = H.noise_chunk(random.Random(0), 30)
        self.assertEqual(len(c), H.CHUNK_BYTES)
        self.assertAlmostEqual(H.rms(c), 30, delta=4)
        self.assertEqual(H.noise_chunk(random.Random(0), 0), b"\0" * H.CHUNK_BYTES)

    def test_mix_clips_and_scales(self):
        out = array("h")
        out.frombytes(H.mix(pcm(4, 30000), pcm(4, 30000)))
        self.assertEqual(list(out), [32767] * 4)
        out = array("h")
        out.frombytes(H.mix(pcm(2, 100), pcm(2, 1000), 0.1))
        self.assertEqual(list(out), [200, 200])

    def test_resample_ratio(self):
        self.assertEqual(len(H.resample_24k_to_16k(pcm(2400))), H.CHUNK_BYTES)


class Routing(unittest.TestCase):
    def test_routes(self):
        self.assertEqual(H.route("both", None), {"doctor", "patient"})
        self.assertEqual(H.route("routed", "patient"), {"patient"})
        self.assertEqual(H.route("routed", None), set())
        with self.assertRaises(ValueError):
            H.route("x", None)


class Scoring(unittest.TestCase):
    def test_tokens(self):
        self.assertEqual(H.tokens("Take 500 mg, twice!", "en"), ["take", "500", "mg", "twice"])
        self.assertEqual(H.tokens("体温38.5度。", "zh"), ["体", "温", "38.5", "度"])
        # Traditional drift and spaced digits are not hearing errors
        self.assertEqual(H.tokens("我沒有過敏", "zh"), H.tokens("我没有过敏", "zh"))
        self.assertEqual(H.tokens("3 8度5", "zh"), H.tokens("38度5", "zh"))

    def test_recall_and_tail(self):
        self.assertEqual(H.recall("a b c d", "a b c d", "en"), 1.0)
        self.assertEqual(H.recall("a b c d", "a b", "en"), 0.5)
        self.assertFalse(H.tail_heard("tell me about your sleep", "tell me about", "en"))
        self.assertTrue(H.tail_heard("我没有过敏。", "我没有过敏", "zh"))

    def test_out_tail(self):
        self.assertTrue(H.out_tail_ok("turn-d5", "如果一周后还发烧，请回来复诊。"))
        self.assertFalse(H.out_tail_ok("turn-d5", "如果一周后发烧还在继续，"))
        self.assertTrue(H.out_tail_ok("c-p1", "I don't have any Allergies."))
        self.assertIsNone(H.out_tail_ok("unknown-d1", "x"))

    def _segs(self):
        return H.build_schedule([("x-d1", "en", "hello there", pcm(16000)),
                                 ("x-p1", "zh", "你好", pcm(16000))], gap_s=2, lead_in_s=0)

    def test_attribution_by_session_and_window(self):
        segs = self._segs()  # doctor 0-1 s, patient 3-4 s
        E = H.Event
        evs = [E(0.5, "doctor", "in", "hello there"), E(1.5, "doctor", "out", "你好"),
               E(3.5, "doctor", "out", "LATE"),        # doctor output during patient clip
               E(3.6, "patient", "out", "hi"),
               E(20.0, "patient", "out", "ghost")]      # far after any patient clip
        by = H.attribute(evs, segs, tail_s=6)
        self.assertEqual([e.text for e in by[0]], ["hello there", "你好", "LATE"])
        self.assertEqual([e.text for e in by[1]], ["hi"])
        self.assertEqual([e.text for e in by[-1]], ["ghost"])

    def test_score_flags_late_spurious_empty(self):
        segs = self._segs()
        E = H.Event
        evs = [E(0.5, "doctor", "in", "hello there"), E(1.2, "doctor", "audio", loud=True),
               E(3.5, "doctor", "out", "迟到"), E(25, "doctor", "out", "鬼")]
        a = H.score(evs, segs, tail_s=6)
        self.assertEqual(a["rows"][0]["late_out_chars"], 2)
        self.assertEqual(a["rows"][0]["first_audio_s"], 1.2)
        self.assertTrue(a["rows"][1]["empty"])
        self.assertEqual(a["agg"]["spurious_out_chars"], 1)
        self.assertEqual(a["agg"]["tails_dropped"], 1)  # patient clip never heard


if __name__ == "__main__":
    unittest.main()
