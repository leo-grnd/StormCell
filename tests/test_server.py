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
        if cls.dbpath and cls.dbpath.exists():
            try:
                cls.dbpath.unlink()
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

    def test_set_home_updates_and_validates(self):
        r = self.client.post("/api/home", json={"lat": 45.0, "lon": 5.0})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        self.assertAlmostEqual(self.client.get("/api/stats").json()["home"]["lat"], 45.0)
        bad = self.client.post("/api/home", json={"lat": 999.0, "lon": 0.0})
        self.assertEqual(bad.status_code, 400)


if __name__ == "__main__":
    unittest.main()
