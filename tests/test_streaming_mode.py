import os
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))

from streaming_mode import default_streaming_mode, parse_streaming_mode  # noqa: E402


def default_with_env(env_value=None):
    old = os.environ.get("STREAMING_MODE")
    try:
        if env_value is None:
            os.environ.pop("STREAMING_MODE", None)
        else:
            os.environ["STREAMING_MODE"] = env_value
        return default_streaming_mode()
    finally:
        if old is None:
            os.environ.pop("STREAMING_MODE", None)
        else:
            os.environ["STREAMING_MODE"] = old


class StreamingModeTests(unittest.TestCase):
    def test_turn_is_default(self):
        self.assertEqual(default_with_env(), "turn")

    def test_realtime_can_come_from_environment(self):
        self.assertEqual(default_with_env("realtime"), "realtime")

    def test_modes_are_normalized_and_validated(self):
        self.assertEqual(parse_streaming_mode(" TURN "), "turn")
        self.assertEqual(parse_streaming_mode("REALTIME"), "realtime")
        with self.assertRaises(ValueError):
            parse_streaming_mode("instant")

    def test_browser_has_distinct_release_paths(self):
        html = (ROOT / "static" / "index.html").read_text()
        self.assertIn('finalized || streamingMode === "realtime"', html)
        self.assertIn('streamingMode === "realtime") render()', html)
        self.assertIn('draining || streamingMode === "realtime"', html)
        self.assertIn('streamingMode === "realtime" ? "turn" : "realtime"', html)


if __name__ == "__main__":
    unittest.main()
