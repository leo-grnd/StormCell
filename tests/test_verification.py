"""Tests de la vérification des prédictions (skill score)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from blitz.verification import arrival_events, evaluate  # noqa: E402


class ArrivalEventTests(unittest.TestCase):
    def test_grouping_by_gap(self):
        ts = [0.0, 60.0, 120.0, 3000.0, 3060.0]  # 2 groupes (écart > 20 min = 1200 s)
        self.assertEqual(arrival_events(ts, gap_min=20), [0.0, 3000.0])

    def test_empty(self):
        self.assertEqual(arrival_events([], gap_min=20), [])


class EvaluateTests(unittest.TestCase):
    def test_hit_false_alarm_and_miss(self):
        in_ring = [1000.0, 1030.0, 5000.0]  # événements à t=1000 et t=5000
        preds = [
            {"ts_made": 400.0, "predicted_arrival": 1060.0, "cell_id": 1, "probability": 0.8},  # hit (≈1000)
            {"ts_made": 200.0, "predicted_arrival": 200.0, "cell_id": 2, "probability": 0.5},   # fausse alerte
        ]
        rep = evaluate(preds, in_ring, tolerance_min=10, gap_min=20)
        self.assertEqual(rep["total_arrivals"], 2)
        self.assertEqual(rep["hits"], 1)
        self.assertEqual(rep["false_alarms"], 1)
        self.assertEqual(rep["misses"], 1)          # l'arrivée à t=5000 n'a pas été prédite
        self.assertEqual(rep["pod"], 0.5)
        self.assertEqual(rep["far"], 0.5)
        self.assertAlmostEqual(rep["mean_eta_error_min"], 1.0, delta=0.01)  # |1000-1060|/60
        self.assertAlmostEqual(rep["mean_lead_min"], 11.0, delta=0.01)      # (1060-400)/60

    def test_no_predictions(self):
        rep = evaluate([], [1000.0], tolerance_min=10, gap_min=20)
        self.assertEqual(rep["total_predictions"], 0)
        self.assertEqual(rep["misses"], 1)
        self.assertIsNone(rep["far"])


if __name__ == "__main__":
    unittest.main()
