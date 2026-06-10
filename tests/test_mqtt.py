"""Tests du parsing des messages MQTT (filtrage par distance, extraction MDS)."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from blitz.config import Config  # noqa: E402
from blitz.mqtt_worker import MqttWorker  # noqa: E402
from blitz.state import SharedState  # noqa: E402


class _FakeMsg:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload


def _msg(d: dict) -> _FakeMsg:
    return _FakeMsg(json.dumps(d).encode())


class OnMessageTests(unittest.TestCase):
    def _worker(self):
        cfg = Config()  # HOME = Mondragon, max_distance_km = 100
        state = SharedState()
        got: list = []
        worker = MqttWorker(cfg, state, on_nearby=got.append)
        return worker, state, got

    def test_nearby_strike_parsed(self):
        worker, state, got = self._worker()
        worker._on_message(None, None, _msg({
            "time": 1_700_000_000_000_000_000, "lat": 44.25, "lon": 4.72, "sig": [1, 2, 3],
        }))
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].mds, 3)                       # len(sig)
        self.assertEqual(state.snapshot_stats()["nearby"], 1)
        self.assertEqual(state.snapshot_stats()["total_world"], 1)

    def test_far_strike_filtered(self):
        worker, state, got = self._worker()
        worker._on_message(None, None, _msg({"time": 1_700_000_000_000_000_000, "lat": 0.0, "lon": 0.0}))
        self.assertEqual(len(got), 0)                          # > 100 km → ignoré
        self.assertEqual(state.snapshot_stats()["total_world"], 1)
        self.assertEqual(state.snapshot_stats()["nearby"], 0)

    def test_garbage_payload_ignored(self):
        worker, state, got = self._worker()
        worker._on_message(None, None, _FakeMsg(b"\xff not json"))
        self.assertEqual(len(got), 0)
        self.assertEqual(state.snapshot_stats()["total_world"], 0)


if __name__ == "__main__":
    unittest.main()
