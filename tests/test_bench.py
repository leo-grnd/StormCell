"""Smoke test du banc d'essai (Lot Perf F3)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from blitz.bench import _run, run_bench  # noqa: E402
from blitz.config import AnalysisConfig  # noqa: E402


class BenchTests(unittest.TestCase):
    def test_run_returns_metrics(self):
        cfg = AnalysisConfig()
        res = _run(cfg, strikes=300, cells=3, ticks=2, radius_km=10.0, seed=1)
        self.assertEqual(res["ticks"], 2)
        self.assertGreaterEqual(res["cells_detected"], 1)
        self.assertGreater(res["mean_ms"], 0.0)
        self.assertGreaterEqual(res["max_ms"], res["median_ms"])

    def test_run_bench_entrypoint(self):
        # Petits paramètres : doit s'exécuter sans erreur et renvoyer 0.
        self.assertEqual(run_bench(strikes=200, cells=2, ticks=1, compare=True), 0)


if __name__ == "__main__":
    unittest.main()
