"""Tests de la persistance SQLite : impacts, cellules, prédictions, analytics."""

from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from blitz.db import Database  # noqa: E402


class _DbCase(unittest.TestCase):
    def setUp(self) -> None:
        self.path = Path(os.environ.get("TEMP", "/tmp")) / f"sc_db_{os.getpid()}_{id(self)}.db"
        for ext in ("", "-wal", "-shm"):
            p = Path(str(self.path) + ext)
            if p.exists():
                p.unlink()
        # gros seuils → on contrôle les flushs via les lectures (qui flushent)
        self.db = Database(self.path, flush_interval=30.0, flush_threshold=10_000)

    def tearDown(self) -> None:
        self.db.close()
        for ext in ("", "-wal", "-shm"):
            p = Path(str(self.path) + ext)
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass

    def _add(self, n: int = 10, dist: float = 5.0, mds: int | None = 6, base_ts: float | None = None) -> None:
        now = base_ts if base_ts is not None else time.time()
        for i in range(n):
            self.db.insert_strike(
                ts_unix=now - i * 60, lat=44 + i * 0.001, lon=4.7,
                distance_km=dist, bearing_deg=(i * 37) % 360, mds=mds,
                home_lat=44.24, home_lon=4.71,
            )


class StrikeTests(_DbCase):
    def test_insert_count_query_batched(self):
        self._add(10)
        self.assertEqual(self.db.count(), 10)        # count() flushe le tampon
        self.assertEqual(len(self.db.query_range(max_distance_km=10)), 10)
        self.assertEqual(len(self.db.query_range(min_mds=99)), 0)

    def test_analytics_queries(self):
        self._add(6)
        self.assertGreaterEqual(len(self.db.count_per_hour(1)), 1)
        self.assertTrue(self.db.count_by_hour_of_day(1))
        self.assertTrue(self.db.count_by_weekday(1))
        self.assertTrue(self.db.bearing_rose(1))
        self.assertTrue(self.db.distance_histogram(1, 10.0, 300.0))

    def test_strike_ts_in_ring(self):
        self._add(5, dist=8.0)    # dans l'anneau
        self._add(5, dist=80.0)   # hors anneau
        now = time.time()
        self.assertEqual(len(self.db.strike_ts_in_ring(15.0, now - 86400, now + 10)), 5)


class CellPersistenceTests(_DbCase):
    def _cell(self, **over):
        base = {
            "cell_id": 1, "first_seen": 100.0, "last_seen": 160.0, "radius_km": 5.0,
            "strikes_count": 12, "flash_rate_per_min": 6.0, "severity": 2.0, "parent_id": None,
            "centroid": {"lat": 44.3, "lon": 4.7}, "velocity_kmh": 30.0, "heading_deg": 90.0,
            "eta_minutes": 20.0, "eta_strike_minutes": 12.0, "closest_approach_km": 8.0,
            "strike_probability": 0.5, "jump_detected": True,
        }
        base.update(over)
        return base

    def test_upsert_keeps_peaks(self):
        run = "run-a"
        self.db.upsert_cells(run, [self._cell()])
        self.db.upsert_cells(run, [self._cell(last_seen=220.0, severity=4.0, strikes_count=20)])
        cells = self.db.list_cells()
        self.assertEqual(len(cells), 1)
        self.assertEqual(cells[0]["max_severity"], 4.0)
        self.assertEqual(cells[0]["last_seen"], 220.0)
        self.assertEqual(cells[0]["total_strikes"], 20)

    def test_track_roundtrip(self):
        run = "run-b"
        self.db.insert_cell_tracks(run, [self._cell()])
        self.db.insert_cell_tracks(run, [self._cell(last_seen=220.0)])
        tr = self.db.cell_track(run, 1)
        self.assertEqual(len(tr), 2)
        self.assertEqual(tr[0]["jump_detected"], 1)
        self.assertAlmostEqual(tr[0]["lat"], 44.3)

    def test_predictions_log_and_query(self):
        self.db.log_predictions([
            {"run_id": "r", "cell_id": 1, "ts_made": 1000.0, "eta": 12.0, "pa": 1720.0, "prob": 0.6, "closest": 8.0},
        ])
        ps = self.db.predictions_between(0.0, 2000.0)
        self.assertEqual(len(ps), 1)
        self.assertEqual(ps[0]["predicted_arrival"], 1720.0)
        self.assertEqual(ps[0]["eta_strike_min"], 12.0)


if __name__ == "__main__":
    unittest.main()
