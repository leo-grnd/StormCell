"""Tests d'intégration des endpoints (FastAPI TestClient, DB temporaire)."""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from blitz.config import load_config  # noqa: E402
from blitz.server import create_app  # noqa: E402
from blitz.state import Strike  # noqa: E402


class ServerEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dbpath = Path(tempfile.gettempdir()) / f"sc_srv_{os.getpid()}.db"
        if cls.dbpath.exists():
            cls.dbpath.unlink()
        # Chemin inexistant → défaults + source_path None : le POST /api/home ne
        # touchera aucun fichier (pas d'effet de bord sur le vrai config.toml).
        cfg = load_config(Path(tempfile.gettempdir()) / "sc_no_such_cfg.toml")
        cfg.db.path = str(cls.dbpath)
        cls.archpath = Path(tempfile.gettempdir()) / f"sc_arch_{os.getpid()}.db"
        cfg.ops.archive_path = str(cls.archpath)
        for ext in ("", "-wal", "-shm"):
            p = Path(str(cls.archpath) + ext)
            if p.exists():
                p.unlink()
        cls.app = create_app(cfg)
        cls.client = TestClient(cls.app)
        cls.client.__enter__()  # déclenche le lifespan (worker MQTT non bloquant)
        ctx = cls.app.state.ctx
        ctx.state.add_strike(Strike(ts_unix=time.time(), lat=44.3, lon=4.7, distance_km=8.0, bearing_deg=90, mds=6))
        ctx.db.geocode_put("44.3,4.7", "Mondragon")
        ctx.db.flush()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.__exit__(None, None, None)
        for base in (cls.dbpath, cls.archpath):
            for ext in ("", "-wal", "-shm"):
                p = Path(str(base) + ext)
                if p.exists():
                    try:
                        p.unlink()
                    except OSError:
                        pass

    def test_stats_has_run_id_and_home(self):
        j = self.client.get("/api/stats").json()
        self.assertIn("run_id", j)
        self.assertIn("home", j)

    def test_analytics_shapes(self):
        j = self.client.get("/api/analytics/summary?days=3650").json()
        self.assertEqual(len(j["hour_of_day"]), 24)
        self.assertEqual(len(j["weekday"]), 7)
        self.assertEqual(len(j["rose"]), 16)

    def test_verification_has_metrics(self):
        j = self.client.get("/api/verification?days=7").json()
        for k in ("pod", "far", "csi", "total_predictions", "total_arrivals", "ring_km"):
            self.assertIn(k, j)

    def test_catalog(self):
        r = self.client.get("/api/cells/catalog?days=30")
        self.assertEqual(r.status_code, 200)
        self.assertIn("current_run", r.json())

    def test_export_csv(self):
        r = self.client.get("/api/export/strikes.csv?limit=10")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.text.startswith("ts_unix,ts_utc"))

    def test_geocode_uses_cache(self):
        j = self.client.get("/api/geocode?lat=44.3&lon=4.7").json()
        self.assertEqual(j["name"], "Mondragon")
        self.assertTrue(j["cached"])

    def test_ops_status_shape(self):
        j = self.client.get("/api/ops/status").json()
        for k in ("archive", "storage", "retention_days", "capture_24h", "continuous_mode"):
            self.assertIn(k, j)
        self.assertIn("disk", j["storage"])

    def test_ops_mode_opens_separate_archive(self):
        ctx = self.app.state.ctx
        r = self.client.post("/api/ops/mode", json={"enabled": True})
        self.assertTrue(r.json()["continuous_mode"])
        self.assertIsNotNone(ctx.archive)
        # double écriture : un impact part dans la base normale ET dans l'archive
        n_main, n_arch = ctx.db.count(), ctx.archive.count()
        ctx.on_nearby(Strike(ts_unix=time.time(), lat=44.31, lon=4.71, distance_km=9.0, bearing_deg=0, mds=6))
        ctx.db.flush()
        ctx.archive.flush()
        self.assertEqual(ctx.db.count(), n_main + 1)
        self.assertEqual(ctx.archive.count(), n_arch + 1)
        # désactivation → l'archive est fermée (mais conservée sur disque)
        self.client.post("/api/ops/mode", json={"enabled": False})
        self.assertIsNone(ctx.archive)

    def test_set_home_updates_and_validates(self):
        r = self.client.post("/api/home", json={"lat": 45.0, "lon": 5.0})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        self.assertAlmostEqual(self.client.get("/api/stats").json()["home"]["lat"], 45.0)
        bad = self.client.post("/api/home", json={"lat": 999.0, "lon": 0.0})
        self.assertEqual(bad.status_code, 400)


if __name__ == "__main__":
    unittest.main()
