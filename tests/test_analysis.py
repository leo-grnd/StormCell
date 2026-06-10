"""Tests de l'analyse des cellules orageuses (DBSCAN + tracking + ETA)."""

from __future__ import annotations

import math
import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from blitz.analysis import update_cells  # noqa: E402
from blitz.config import AnalysisConfig, HomeConfig  # noqa: E402
from blitz.geo import unproject_local  # noqa: E402
from blitz.state import Strike  # noqa: E402

HOME = HomeConfig(lat=44.243318, lon=4.716102)
CFG = AnalysisConfig(cluster_eps_km=8.0, cluster_min_samples=3, cell_window_minutes=30)


def make_strike(x_km: float, y_km: float, ts: float) -> Strike:
    lat, lon = unproject_local(HOME.lat, HOME.lon, x_km, y_km)
    return Strike(ts_unix=ts, lat=lat, lon=lon, distance_km=math.hypot(x_km, y_km), bearing_deg=0, mds=5)


class ClusteringTests(unittest.TestCase):
    def test_three_distinct_clusters(self):
        rng = random.Random(42)
        strikes: list[Strike] = []
        # 3 clusters bien séparés (>20 km), 6 impacts chacun.
        centers = [(20, 0), (-15, 20), (0, -25)]
        ts = 1_000_000.0
        for cx, cy in centers:
            for _ in range(6):
                dx = rng.uniform(-2, 2)
                dy = rng.uniform(-2, 2)
                strikes.append(make_strike(cx + dx, cy + dy, ts))
                ts += 5
        # Quelques impacts isolés (bruit)
        for _ in range(3):
            strikes.append(make_strike(rng.uniform(-50, 50), rng.uniform(-50, 50), ts))
            ts += 5

        cells, _ = update_cells(HOME, strikes, previous={}, cfg=CFG, now=ts, next_id=1)
        self.assertEqual(len(cells), 3, f"expected 3 cells, got {len(cells)}: {cells}")

    def test_below_min_samples_returns_empty(self):
        ts = 1_000_000.0
        strikes = [make_strike(0, 0, ts), make_strike(1, 1, ts + 1)]  # 2 < min_samples=3
        cells, _ = update_cells(HOME, strikes, previous={}, cfg=CFG, now=ts)
        self.assertEqual(cells, {})


class TrackingTests(unittest.TestCase):
    def test_cell_id_persists_when_drifting(self):
        ts = 1_000_000.0
        prev: dict = {}
        next_id = 1
        cell_ids: list[int] = []
        # Cellule qui dérive de (10,0) vers (16,0) sur 3 ticks (déplacement de 3 km, < 2*eps).
        for tick in range(3):
            cx = 10.0 + tick * 3.0
            strikes = [
                make_strike(cx - 1, 0, ts),
                make_strike(cx, 0.5, ts + 2),
                make_strike(cx + 1, -0.5, ts + 4),
                make_strike(cx, 1, ts + 6),
            ]
            cells, next_id = update_cells(HOME, strikes, previous=prev, cfg=CFG, now=ts + 10, next_id=next_id)
            self.assertEqual(len(cells), 1)
            cell_ids.append(next(iter(cells.keys())))
            prev = cells
            ts += 60  # 1 minute par tick

        self.assertEqual(len(set(cell_ids)), 1, f"id should persist, got {cell_ids}")


class ETATests(unittest.TestCase):
    def test_approaching_cell_has_eta(self):
        """Cellule qui s'approche de HOME → ETA défini, distance d'approche faible."""
        # Cellule à (30, 0), se déplaçant vers (0,0) à ~30 km/h = 0.5 km/min.
        # 5 ticks de 60 s.
        prev: dict = {}
        next_id = 1
        cells: dict = {}
        ts = 0.0
        for tick in range(5):
            cx = 30.0 - tick * 0.5  # km
            strikes = [
                make_strike(cx + 0.5, 0.2, ts),
                make_strike(cx - 0.5, -0.2, ts + 1),
                make_strike(cx, 0.0, ts + 2),
                make_strike(cx + 0.3, 0.6, ts + 3),
            ]
            cells, next_id = update_cells(HOME, strikes, previous=prev, cfg=CFG, now=ts + 5, next_id=next_id)
            prev = cells
            ts += 60

        self.assertEqual(len(cells), 1)
        cell = next(iter(cells.values()))
        self.assertIsNotNone(cell.eta_minutes)
        self.assertIsNotNone(cell.velocity_kmh)
        self.assertLess(cell.closest_approach_km, 5.0)  # passe près de HOME

    def test_receding_cell_has_no_eta(self):
        """Cellule qui s'éloigne → eta_minutes doit être None."""
        prev: dict = {}
        next_id = 1
        cells: dict = {}
        ts = 0.0
        for tick in range(5):
            cx = 10.0 + tick * 0.5  # s'éloigne vers l'est
            strikes = [
                make_strike(cx + 0.5, 0.2, ts),
                make_strike(cx - 0.5, -0.2, ts + 1),
                make_strike(cx, 0.0, ts + 2),
                make_strike(cx + 0.3, 0.6, ts + 3),
            ]
            cells, next_id = update_cells(HOME, strikes, previous=prev, cfg=CFG, now=ts + 5, next_id=next_id)
            prev = cells
            ts += 60

        self.assertEqual(len(cells), 1)
        cell = next(iter(cells.values()))
        self.assertIsNone(cell.eta_minutes)


class GlobalDetectionTests(unittest.TestCase):
    """Vérifie que la métrique haversine permet la détection loin de HOME."""

    def test_cluster_detected_on_other_continent(self):
        # Cluster centré à Sydney (-33.87, 151.21), bien loin de Mondragon.
        ts = 1_000_000.0
        strikes: list[Strike] = []
        rng = random.Random(7)
        for _ in range(8):
            dlat = rng.uniform(-0.04, 0.04)  # ~4 km
            dlon = rng.uniform(-0.04, 0.04)
            strikes.append(Strike(
                ts_unix=ts, lat=-33.87 + dlat, lon=151.21 + dlon,
                distance_km=16_700, bearing_deg=0, mds=5,
            ))
            ts += 3
        cells, _ = update_cells(HOME, strikes, previous={}, cfg=CFG, now=ts)
        self.assertEqual(len(cells), 1)
        c = next(iter(cells.values()))
        self.assertAlmostEqual(c.centroid_lat, -33.87, delta=0.1)
        self.assertAlmostEqual(c.centroid_lon, 151.21, delta=0.1)
        # ETA HOME interdit (au-delà de eta_max_radius_km)
        self.assertIsNone(c.eta_minutes)


class EdgeEtaTests(unittest.TestCase):
    def test_strike_eta_precedes_centroid_eta(self):
        """Une cellule qui fonce sur HOME doit avoir un ETA-bord ≤ ETA-centroïde."""
        prev: dict = {}
        next_id = 1
        cells: dict = {}
        ts = 0.0
        for tick in range(6):
            cx = 60.0 - tick * 6.0  # se rapproche vite de HOME (origine), plein ouest
            strikes = [
                make_strike(cx + 0.5, 0.2, ts),
                make_strike(cx - 0.5, -0.2, ts + 1),
                make_strike(cx, 0.0, ts + 2),
                make_strike(cx + 0.3, 0.6, ts + 3),
            ]
            cells, next_id = update_cells(HOME, strikes, previous=prev, cfg=CFG, now=ts + 5, next_id=next_id)
            prev = cells
            ts += 60
        cell = next(iter(cells.values()))
        self.assertIsNotNone(cell.eta_minutes)
        self.assertIsNotNone(cell.eta_strike_minutes)
        self.assertLessEqual(cell.eta_strike_minutes, cell.eta_minutes + 1e-6)
        # proba de coup définie et dans [0,1]
        self.assertIsNotNone(cell.strike_probability)
        self.assertTrue(0.0 <= cell.strike_probability <= 1.0)
        # taux d'éclairs instantané renseigné
        self.assertIsNotNone(cell.flash_rate_per_min)


class LineageTests(unittest.TestCase):
    def test_split_sets_parent_id(self):
        # tick 1 : une cellule unique autour de (20, 0)
        ts = 1_000_000.0
        strikes1 = [make_strike(20 + (i - 2) * 0.5, 0.0, ts + i) for i in range(5)]
        cells1, next_id = update_cells(HOME, strikes1, previous={}, cfg=CFG, now=ts + 10, next_id=1)
        self.assertEqual(len(cells1), 1)
        parent_id = next(iter(cells1))

        # tick 2 : deux clusters distincts — l'un continue (20,0), l'autre naît (20,12)
        ts += 60
        strikes2 = [make_strike(20 + (i - 2) * 0.5, 0.0, ts + i) for i in range(5)]
        strikes2 += [make_strike(20 + (i - 2) * 0.5, 12.0, ts + i) for i in range(5)]
        cells2, _ = update_cells(HOME, strikes2, previous=cells1, cfg=CFG, now=ts + 10, next_id=next_id)
        self.assertEqual(len(cells2), 2)
        # la cellule mère garde son id ; la nouvelle pointe vers elle
        new_cell = next(c for cid, c in cells2.items() if cid != parent_id)
        self.assertEqual(new_cell.parent_id, parent_id)


class PersistenceTests(unittest.TestCase):
    """La persistance fade-out doit garder une cellule sur quelques ticks creux."""

    def test_ghost_survives_one_miss_then_disappears(self):
        # tick 1 : cellule présente
        ts = 0.0
        strikes = [make_strike(20 + 0.2 * i, 0.1 * i, ts + i) for i in range(5)]
        cells, next_id = update_cells(HOME, strikes, previous={}, cfg=CFG, now=ts + 10, next_id=1)
        self.assertEqual(len(cells), 1)
        cid = next(iter(cells))

        # tick 2 : aucun nouvel impact (fenêtre vide) → la cellule doit survivre (misses=1)
        cells2, next_id = update_cells(HOME, [], previous=cells, cfg=CFG, now=ts + 70, next_id=next_id)
        self.assertIn(cid, cells2)
        self.assertEqual(cells2[cid].misses, 1)

        # tick 3 : encore rien → misses=2 (= max_track_misses dans CFG par défaut)
        cells3, next_id = update_cells(HOME, [], previous=cells2, cfg=CFG, now=ts + 140, next_id=next_id)
        self.assertIn(cid, cells3)
        self.assertEqual(cells3[cid].misses, 2)

        # tick 4 : misses passerait à 3 > max_track_misses → la cellule disparaît
        cells4, _ = update_cells(HOME, [], previous=cells3, cfg=CFG, now=ts + 210, next_id=next_id)
        self.assertNotIn(cid, cells4)


class IntensityTrendTests(unittest.TestCase):
    def test_intensifying_cell_marked_growing(self):
        # Plus de plus en plus d'impacts par tick → intensité en hausse
        prev: dict = {}
        next_id = 1
        ts = 0.0
        cells: dict = {}
        for tick in range(6):
            n_strikes = 4 + tick * 2
            strikes = [make_strike(10 + (i - n_strikes/2) * 0.3, 0.0, ts + i) for i in range(n_strikes)]
            cells, next_id = update_cells(HOME, strikes, previous=prev, cfg=CFG, now=ts + 30, next_id=next_id)
            prev = cells
            ts += 60
        self.assertEqual(len(cells), 1)
        cell = next(iter(cells.values()))
        self.assertEqual(cell.intensity_trend, "growing")


class ConfidenceTests(unittest.TestCase):
    def test_confidence_grows_with_track_length(self):
        prev: dict = {}
        next_id = 1
        ts = 0.0
        confidences = []
        for tick in range(6):
            cx = 15.0 - tick * 0.5
            strikes = [make_strike(cx + (i - 2) * 0.4, 0.1 * i, ts + i) for i in range(5)]
            cells, next_id = update_cells(HOME, strikes, previous=prev, cfg=CFG, now=ts + 10, next_id=next_id)
            prev = cells
            ts += 60
            if cells:
                confidences.append(next(iter(cells.values())).confidence)
        # Le score doit augmenter avec la longueur du track
        self.assertLess(confidences[0], confidences[-1])
        self.assertGreater(confidences[-1], 0.5)


if __name__ == "__main__":
    unittest.main()
