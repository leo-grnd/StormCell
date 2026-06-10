"""Tests du module de tracking avancé : Kalman, lightning jump, sévérité, nowcast."""

from __future__ import annotations

import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from blitz.tracking import (  # noqa: E402
    CVKalman,
    lightning_jump,
    severity_index,
    strike_probability,
)

MEAS_VAR = 2.5 ** 2
ACCEL_VAR = 2e-5 ** 2


class KalmanTests(unittest.TestCase):
    def test_converges_to_constant_velocity(self):
        # Trajectoire vraie : 36 km/h plein est (vx = 0.01 km/s).
        kf = CVKalman.init(0.0, 0.0, MEAS_VAR, ACCEL_VAR)
        vx_true, vy_true = 0.01, 0.0
        dt = 10.0
        rng = random.Random(0)
        for k in range(1, 50):
            kf.predict(dt)
            x = vx_true * (k * dt) + rng.gauss(0, 0.5)
            y = vy_true * (k * dt) + rng.gauss(0, 0.5)
            kf.update(x, y)
        vx, vy = kf.vel
        self.assertAlmostEqual(vx, vx_true, delta=0.002)  # < ~7 km/h d'erreur
        self.assertAlmostEqual(vy, vy_true, delta=0.002)
        self.assertLess(kf.pos_std_km(), 5.0)

    def test_state_roundtrip(self):
        kf = CVKalman.init(1.0, 2.0, MEAS_VAR, ACCEL_VAR)
        kf.predict(10.0)
        kf.update(1.1, 2.1)
        kf2 = CVKalman.from_state(kf.state_list(), kf.cov_list(), MEAS_VAR, ACCEL_VAR)
        self.assertEqual(kf.state_list(), kf2.state_list())
        self.assertEqual(kf.cov_list(), kf2.cov_list())


class LightningJumpTests(unittest.TestCase):
    def test_no_jump_on_steady_rate(self):
        rng = random.Random(1)
        t0 = 1_000_000.0
        hist = [(t0 + i * 60, 3.0 + rng.gauss(0, 0.3)) for i in range(10)]
        detected, _ = lightning_jump(hist)
        self.assertFalse(detected)

    def test_jump_fires_on_surge(self):
        rng = random.Random(2)
        t0 = 1_000_000.0
        hist = [(t0 + i * 60, 3.0 + rng.gauss(0, 0.3)) for i in range(9)]
        hist.append((t0 + 9 * 60, 22.0))  # flambée soudaine
        detected, level = lightning_jump(hist)
        self.assertTrue(detected)
        self.assertGreaterEqual(level, 2.0)

    def test_too_few_points(self):
        detected, level = lightning_jump([(0.0, 5.0), (60.0, 9.0)])
        self.assertFalse(detected)
        self.assertEqual(level, 0.0)


class SeverityTests(unittest.TestCase):
    def test_bounds_and_ordering(self):
        low = severity_index(1.0, 5.0, 5, "stable", False)
        high = severity_index(25.0, 30.0, 200, "growing", True)
        self.assertTrue(0.0 <= low <= 5.0)
        self.assertTrue(0.0 <= high <= 5.0)
        self.assertGreater(high, low)


class StrikeProbabilityTests(unittest.TestCase):
    def test_bounds_and_monotonic(self):
        p_near = strike_probability(2.0, 10.0, pos_std_km=3.0, effective_radius_km=15.0)
        p_far = strike_probability(40.0, 10.0, pos_std_km=3.0, effective_radius_km=15.0)
        self.assertTrue(0.0 <= p_far <= p_near <= 1.0)
        self.assertGreater(p_near, 0.9)  # 2 km bien à l'intérieur de l'anneau 15 km

    def test_zero_beyond_horizon(self):
        self.assertEqual(
            strike_probability(2.0, 60.0, 3.0, 15.0, horizon_min=30.0), 0.0
        )

    def test_zero_when_no_eta(self):
        self.assertEqual(strike_probability(None, None, 3.0, 15.0), 0.0)


if __name__ == "__main__":
    unittest.main()
