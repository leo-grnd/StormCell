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


# ── Lot D #19 : historique décimé ────────────────────────────────────────────
class DecimationTests(_DbCase):
    def test_count_range_and_decimation(self):
        now = time.time()
        # 40 impacts répartis sur 4 mailles distinctes (10 par maille).
        for cell, (dlat, dlon) in enumerate([(0.0, 0.0), (0.5, 0.0), (0.0, 0.5), (0.5, 0.5)]):
            for i in range(10):
                self.db.insert_strike(
                    ts_unix=now - i, lat=44.0 + dlat + i * 1e-4, lon=4.0 + dlon,
                    distance_km=5.0 + cell, bearing_deg=0, mds=6, home_lat=44.0, home_lon=4.0,
                )
        self.assertEqual(self.db.count_range(), 40)
        agg = self.db.query_range_decimated(0.1)   # maille ≈ 11 km → 4 amas
        self.assertEqual(len(agg), 4)
        self.assertEqual(sum(r["n"] for r in agg), 40)   # aucun impact perdu
        self.assertTrue(all("distance_km" in r for r in agg))


# ── Lot D #20 : rétention / index / maintenance ──────────────────────────────
class MaintenanceTests(_DbCase):
    def test_composite_index_present(self):
        rows = self.db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_strikes_ts_dist'"
        ).fetchall()
        self.assertEqual(len(rows), 1)

    def test_purge_older_than(self):
        now = time.time()
        self._add(5, base_ts=now)              # récents (now, now-60, …)
        self._add(5, base_ts=now - 40 * 86400)  # vieux de ~40 jours
        self.assertEqual(self.db.count(), 10)
        deleted = self.db.purge_older_than(now - 10 * 86400)
        self.assertEqual(deleted, 5)
        self.assertEqual(self.db.count(), 5)

    def test_maintain_checkpoints(self):
        self._add(3)
        res = self.db.maintain()
        self.assertTrue(res["checkpointed"])
        self.assertEqual(res["deleted"], 0)   # retention_days=0 → aucune purge

    def test_purge_all(self):
        self._add(6)
        self.db.geocode_put("k", "v")
        self.assertEqual(self.db.count(), 6)
        n = self.db.purge_all()
        self.assertEqual(n, 6)
        self.assertEqual(self.db.count(), 0)              # tout est vidé
        self.assertIsNone(self.db.geocode_get("k"))       # géocache compris

    def test_storage_info(self):
        self._add(4)
        info = self.db.storage_info()
        self.assertIn("db_bytes", info)
        self.assertIn("wal_bytes", info)
        self.assertGreaterEqual(info["db_bytes"], 0)
        self.assertGreaterEqual(info["disk"]["total"], 0)

    def test_backup_roundtrip(self):
        import sqlite3
        self._add(5)
        dest = Path(str(self.path) + ".bak")
        if dest.exists():
            dest.unlink()
        n = self.db.backup(str(dest))
        self.assertGreater(n, 0)
        conn = sqlite3.connect(str(dest))
        cnt = conn.execute("SELECT COUNT(*) FROM strikes").fetchone()[0]
        conn.close()
        dest.unlink()
        self.assertEqual(cnt, 5)   # la sauvegarde contient bien les impacts


if __name__ == "__main__":
    unittest.main()
