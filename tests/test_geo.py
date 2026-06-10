"""Tests unitaires des fonctions géométriques."""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from blitz.geo import (  # noqa: E402
    bearing,
    cardinal,
    color_for_distance,
    haversine,
    project_local,
    unproject_local,
)

# Coordonnées de référence
PARIS = (48.8566, 2.3522)
MARSEILLE = (43.2965, 5.3698)
MONDRAGON = (44.243318, 4.716102)


class HaversineTests(unittest.TestCase):
    def test_paris_marseille(self):
        d = haversine(*PARIS, *MARSEILLE)
        # Distance grand-cercle référence ≈ 661 km (± 2 km de marge)
        self.assertAlmostEqual(d, 661, delta=2)

    def test_zero_distance(self):
        self.assertEqual(haversine(*PARIS, *PARIS), 0.0)

    def test_symmetry(self):
        d1 = haversine(*PARIS, *MARSEILLE)
        d2 = haversine(*MARSEILLE, *PARIS)
        self.assertAlmostEqual(d1, d2, delta=1e-9)


class BearingTests(unittest.TestCase):
    def test_paris_to_marseille(self):
        # Marseille est SSE de Paris → azimut autour de 158-160°
        b = bearing(*PARIS, *MARSEILLE)
        self.assertTrue(150 <= b <= 165, f"bearing={b}")

    def test_due_north(self):
        b = bearing(0.0, 0.0, 1.0, 0.0)
        self.assertAlmostEqual(b, 0.0, delta=1e-6)

    def test_due_east(self):
        b = bearing(0.0, 0.0, 0.0, 1.0)
        self.assertAlmostEqual(b, 90.0, delta=1e-6)


class CardinalTests(unittest.TestCase):
    def test_main_directions(self):
        self.assertEqual(cardinal(0), "N")
        self.assertEqual(cardinal(90), "E")
        self.assertEqual(cardinal(180), "S")
        self.assertEqual(cardinal(270), "O")

    def test_wraparound(self):
        self.assertEqual(cardinal(360), "N")
        self.assertEqual(cardinal(11), "N")
        self.assertEqual(cardinal(12), "NNE")


class ColorTests(unittest.TestCase):
    def test_thresholds(self):
        self.assertEqual(color_for_distance(5), "bold red")
        self.assertEqual(color_for_distance(20), "orange3")
        self.assertEqual(color_for_distance(100), "yellow")
        self.assertEqual(color_for_distance(300), "green")


class ProjectionTests(unittest.TestCase):
    def test_roundtrip(self):
        # Un point proche de HOME doit revenir au même endroit après aller-retour.
        target = (MONDRAGON[0] + 0.5, MONDRAGON[1] - 0.3)
        x, y = project_local(*MONDRAGON, *target)
        lat, lon = unproject_local(*MONDRAGON, x, y)
        self.assertAlmostEqual(lat, target[0], delta=1e-4)
        self.assertAlmostEqual(lon, target[1], delta=1e-4)

    def test_distance_consistency(self):
        # La norme locale doit approcher haversine à courte distance.
        target = (MONDRAGON[0] + 0.4, MONDRAGON[1] + 0.4)  # ~50 km
        x, y = project_local(*MONDRAGON, *target)
        local_d = math.hypot(x, y)
        true_d = haversine(*MONDRAGON, *target)
        self.assertAlmostEqual(local_d, true_d, delta=true_d * 0.01)  # 1% de tolérance


if __name__ == "__main__":
    unittest.main()
