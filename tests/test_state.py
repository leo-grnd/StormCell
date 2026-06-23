"""Tests de l'état partagé : métriques de débit & santé (Lot D #21)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from blitz.state import SharedState, Strike  # noqa: E402


def _strike(dist: float = 5.0) -> Strike:
    return Strike(ts_unix=1000.0, lat=44.2, lon=4.7, distance_km=dist, bearing_deg=0, mds=6)


class ThroughputTests(unittest.TestCase):
    def test_drop_counter(self):
        st = SharedState(max_strikes_recent=100)
        st.record_drop()
        st.record_drop()
        self.assertEqual(st.snapshot_stats()["queue_dropped"], 2)

    def test_cells_metrics(self):
        st = SharedState()
        st.set_cells_metrics(12.34, 3)
        snap = st.snapshot_stats()
        self.assertEqual(snap["cells_compute_ms"], 12.3)
        self.assertEqual(snap["cells_count"], 3)

    def test_buffer_fill_reported(self):
        st = SharedState(max_strikes_recent=10)
        for _ in range(4):
            st.add_strike(_strike())
        snap = st.snapshot_stats()
        self.assertEqual(snap["recent_buffer"], 4)
        self.assertEqual(snap["recent_buffer_max"], 10)
        # Les clés de débit existent toujours (valeurs lissées, éventuellement None).
        self.assertIn("world_per_s", snap)
        self.assertIn("nearby_per_min", snap)

    def test_prune_beyond_ring(self):
        st = SharedState(max_strikes_recent=100)
        st.add_strike(_strike(dist=5.0))
        st.add_strike(_strike(dist=500.0))
        st.prune_beyond(150.0)
        self.assertEqual(st.snapshot_stats()["recent_buffer"], 1)


if __name__ == "__main__":
    unittest.main()
