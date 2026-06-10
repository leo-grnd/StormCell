"""Tests du chargement de config et de la réécriture de HOME."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from blitz.config import load_config, update_home  # noqa: E402


class LoadConfigTests(unittest.TestCase):
    def test_defaults(self):
        # Chemin inexistant → valeurs par défaut (ne pas passer None : retomberait
        # sur ./config.toml si présent dans le cwd).
        c = load_config(Path(tempfile.gettempdir()) / "sc_no_such_config_xyz.toml")
        self.assertEqual(c.filter.max_distance_km, 100.0)
        self.assertEqual(c.analysis.cluster_min_samples, 4)
        self.assertEqual(c.predict.min_probability, 0.3)
        self.assertIsNone(c.source_path)

    def test_merge_keeps_unset_defaults(self):
        p = Path(tempfile.gettempdir()) / f"sc_cfg_{os.getpid()}.toml"
        p.write_text("[home]\nlat = 1.5\nlon = 2.5\n[filter]\nmax_distance_km = 42\n", encoding="utf-8")
        try:
            c = load_config(p)
            self.assertEqual(c.home.lat, 1.5)
            self.assertEqual(c.filter.max_distance_km, 42)
            self.assertEqual(c.analysis.cluster_eps_km, 12.0)  # défaut conservé
        finally:
            p.unlink()


class UpdateHomeTests(unittest.TestCase):
    def test_rewrites_and_preserves_comment(self):
        p = Path(tempfile.gettempdir()) / f"sc_cfg2_{os.getpid()}.toml"
        p.write_text("[home]\nlat = 1.0   # Mondragon\nlon = 2.0\n\n[web]\nport = 8000\n", encoding="utf-8")
        try:
            self.assertTrue(update_home(p, 48.85, 2.35))
            c = load_config(p)
            self.assertAlmostEqual(c.home.lat, 48.85)
            self.assertAlmostEqual(c.home.lon, 2.35)
            self.assertEqual(c.web.port, 8000)              # reste du fichier intact
            self.assertIn("# Mondragon", p.read_text(encoding="utf-8"))  # commentaire conservé
        finally:
            p.unlink()

    def test_missing_file_returns_false(self):
        self.assertFalse(update_home(None, 1.0, 2.0))


if __name__ == "__main__":
    unittest.main()
