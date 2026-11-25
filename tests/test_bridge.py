"""Bridge tests: the real /ws handler and _direction loop against a FAKE Gemini
client (no network, no key). Verifies the starter-style input contract:

- audio reaches ONLY the routed direction, as 16 kHz PCM blobs, in order
- releasing / switching the route sends audio_stream_end to the side losing it
- no manual activity markers are ever sent
- model output is tagged with the side whose session produced it
- go_away reconnects with the resumption handle

Run: uv run --with fastapi --with httpx --with websockets --with uvicorn --with google-genai \
       python -m unittest tests.test_bridge
"""
import asyncio
import base64
import os
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("GEMINI_API_KEY", "test-key")

import server  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def msg(**kw):
    base = dict(session_resumption_update=None, go_away=None, server_content=None)
    base.update(kw)
    return NS(**base)


def content(inp=None, out=None, audio=None, done=False):
    parts = [NS(inline_data=NS(data=audio, mime_type="audio/pcm;rate=24000"))] if audio else []
    return NS(input_transcription=NS(text=inp) if inp else None,
              output_transcription=NS(text=out) if out else None,
              model_turn=NS(parts=parts) if parts else None,
              turn_complete=done)


class FakeSession:
    def __init__(self, world, target, handle):
        self.world, self.target, self.handle = world, target, handle
        self.q = asyncio.Queue()
        self.sent = []
        world.sessions.append(self)
        self.q.put_nowait(msg(session_resumption_update=NS(new_handle=f"h-{target}-{len(world.sessions)}")))

    async def send_realtime_input(self, **kw):
        self.sent.append(kw)
        if "audio" in kw:
            n = sum(1 for x in self.sent if "audio" in x)
            if n == 1:  # first chunk of a stream: answer like the model would
                await self.q.put(msg(server_content=content(inp="src", out=f"to-{self.target}",
                                                             audio=b"\x00\x10" * 2400)))
            if self.world.go_away_after and n == self.world.go_away_after:
                self.world.go_away_after = None
                await self.q.put(msg(go_away=NS(time_left="1s")))
        if kw.get("audio_stream_end"):
            await self.q.put(msg(server_content=content(out=f"flush-{self.target}")))

    async def receive(self):
        while True:
            m = await self.q.get()
            yield m
            if m.go_away:
                return


class FakeWorld:
    def __init__(self):
        self.sessions = []
        self.configs = []
        self.go_away_after = None
        world = self

        class _Connect:
            def __init__(self, model, config):
                world.configs.append(config)
                tgt = config.translation_config.target_language_code
                h = config.session_resumption.handle
                self.s = FakeSession(world, tgt, h)

            async def __aenter__(self):
                return self.s

            async def __aexit__(self, *a):
                return False

        self.client = NS(aio=NS(live=NS(connect=_Connect)))

    def by_target(self, tgt):
        return [s for s in self.sessions if s.target == tgt]


def chunk(v=1):
    return base64.b64encode(bytes([v, 0]) * 1600).decode()  # 100 ms of 16 kHz PCM16


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.world = FakeWorld()
        self._orig = server.gemini_client
        server.gemini_client = lambda: self.world.client

    def tearDown(self):
        server.gemini_client = self._orig

    def open(self, ws):
        ws.send_json({"type": "config", "doctor": "en", "patient": "zh"})
        self.assertEqual(ws.receive_json()["type"], "ready")

    def wait_for(self, pred, timeout=3.0):
        end = time.time() + timeout
        while time.time() < end:
            if pred():
                return True
            time.sleep(0.02)
        return False

    def drain(self, ws, want, timeout=3.0):
        """Read messages until `want(list)` holds."""
        got = []
        end = time.time() + timeout
        while time.time() < end and not want(got):
            got.append(ws.receive_json())
        return got

    def test_config_is_starter_style(self):
        with TestClient(server.app).websocket_connect("/ws") as ws:
            self.open(ws)
            self.assertTrue(self.wait_for(lambda: len(self.world.configs) == 2))
            for c in self.world.configs:
                self.assertIsNone(c.realtime_input_config)
                self.assertIsNone(c.context_window_compression)
            self.assertEqual({c.translation_config.target_language_code for c in self.world.configs},
                             {"zh-Hans", "en"})

    def test_audio_only_reaches_routed_side_and_release_flushes(self):
        with TestClient(server.app).websocket_connect("/ws") as ws:
            self.open(ws)
            ws.send_json({"type": "route", "side": "doctor"})
            for v in range(1, 6):
                ws.send_json({"type": "audio", "b64": chunk(v)})
            ws.send_json({"type": "route", "side": None})
            ws.send_json({"type": "audio", "b64": chunk(9)})  # released: dropped
            got = self.drain(ws, lambda g: any(m.get("text") == "flush-zh-Hans" for m in g))
            doc = self.world.by_target("zh-Hans")[0]
            pat = self.world.by_target("en")[0]
            audio = [kw["audio"] for kw in doc.sent if "audio" in kw]
            self.assertEqual([a.data[0] for a in audio], [1, 2, 3, 4, 5], "in order, nothing else")
            self.assertTrue(all(a.mime_type == "audio/pcm;rate=16000" and len(a.data) == 3200
                                for a in audio), "100 ms 16 kHz PCM16 blobs")
            self.assertEqual(doc.sent[-1], {"audio_stream_end": True}, "release flushes")
            self.assertEqual(pat.sent, [], "unrouted side receives nothing")
            for kw in doc.sent:
                self.assertNotIn("activity_start", kw)
                self.assertNotIn("activity_end", kw)
            tagged = [m for m in got if m["type"] in ("source", "translation", "audio")]
            self.assertTrue(tagged and all(m["side"] == "doctor" for m in tagged))
            self.assertTrue(all("turn" not in m for m in got), "no turn tagging")

    def test_preroll_carries_speech_onset_into_new_route(self):
        with TestClient(server.app).websocket_connect("/ws") as ws:
            self.open(ws)
            for v in (10, 11, 12, 13):  # unrouted mic: only the last 3 are kept
                ws.send_json({"type": "audio", "b64": chunk(v)})
            ws.send_json({"type": "route", "side": "patient"})
            ws.send_json({"type": "audio", "b64": chunk(14)})
            ws.send_json({"type": "route", "side": "doctor"})  # pre-roll is spent
            ws.send_json({"type": "audio", "b64": chunk(15)})
            self.assertTrue(self.wait_for(lambda: self.world.by_target("zh-Hans") and
                                          any("audio" in kw for kw in self.world.by_target("zh-Hans")[0].sent)))
            pat = self.world.by_target("en")[0]
            doc = self.world.by_target("zh-Hans")[0]
            self.assertEqual([kw["audio"].data[0] for kw in pat.sent if "audio" in kw], [11, 12, 13, 14])
            self.assertEqual([kw["audio"].data[0] for kw in doc.sent if "audio" in kw], [15],
                             "routed audio is not reused as pre-roll")

    def test_switch_flushes_previous_and_tags_each_side(self):
        with TestClient(server.app).websocket_connect("/ws") as ws:
            self.open(ws)
            ws.send_json({"type": "route", "side": "doctor"})
            ws.send_json({"type": "audio", "b64": chunk()})
            ws.send_json({"type": "route", "side": "patient"})
            ws.send_json({"type": "audio", "b64": chunk()})
            got = self.drain(ws, lambda g: {m.get("text") for m in g} >= {"to-zh-Hans", "to-en", "flush-zh-Hans"})
            sides = {m["text"]: m["side"] for m in got if m["type"] == "translation"}
            self.assertEqual(sides["to-zh-Hans"], "doctor")
            self.assertEqual(sides["flush-zh-Hans"], "doctor")
            self.assertEqual(sides["to-en"], "patient")
            pat = self.world.by_target("en")[0]
            self.assertNotIn({"audio_stream_end": True}, pat.sent, "the side gaining the mic is not flushed")

    def test_go_away_reconnects_with_handle(self):
        self.world.go_away_after = 2
        with TestClient(server.app).websocket_connect("/ws") as ws:
            self.open(ws)
            ws.send_json({"type": "route", "side": "doctor"})
            for _ in range(2):
                ws.send_json({"type": "audio", "b64": chunk()})
            self.assertTrue(self.wait_for(lambda: len(self.world.by_target("zh-Hans")) == 2, 4))
            second = self.world.by_target("zh-Hans")[1]
            self.assertTrue(second.handle and second.handle.startswith("h-zh-Hans"), "resumed with handle")
            ws.send_json({"type": "audio", "b64": chunk(7)})
            self.assertTrue(self.wait_for(lambda: any("audio" in kw for kw in second.sent)),
                            "audio flows to the resumed session")

    def test_malformed_audio_is_ignored(self):
        with TestClient(server.app).websocket_connect("/ws") as ws:
            self.open(ws)
            ws.send_json({"type": "route", "side": "doctor"})
            ws.send_json({"type": "audio", "b64": "!!notbase64"})
            ws.send_json({"type": "audio", "b64": base64.b64encode(b"\x01").decode()})  # odd length
            ws.send_json({"type": "audio", "b64": 5})
            ws.send_json({"type": "audio", "b64": chunk(3)})
            self.assertTrue(self.wait_for(lambda: self.world.by_target("zh-Hans")
                                          and any("audio" in kw for kw in self.world.by_target("zh-Hans")[0].sent)))
            doc = self.world.by_target("zh-Hans")[0]
            self.assertEqual([kw["audio"].data[0] for kw in doc.sent if "audio" in kw], [3])


if __name__ == "__main__":
    unittest.main()
