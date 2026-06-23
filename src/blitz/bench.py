"""Banc d'essai : rejoue un orage synthétique lourd et chronomètre le pipeline d'analyse.

Sert à détecter les régressions de performance du clustering + tracking
(`update_cells`) et à mesurer le gain de la pré-agrégation grille (#18). Aucune
dépendance réseau : tout est généré en mémoire.

Usage :
    python -m blitz bench --strikes 15000 --cells 8 --ticks 12
    python -m blitz bench --compare        # grille ON vs OFF, ratio d'accélération
"""

from __future__ import annotations

import math
import random
import time
from dataclasses import replace

from .analysis import update_cells
from .config import AnalysisConfig, HomeConfig
from .geo import unproject_local
from .state import Cell, Strike

_HOME = HomeConfig(lat=44.243318, lon=4.716102)


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = min(len(s) - 1, max(0, int(round((pct / 100.0) * (len(s) - 1)))))
    return s[k]


def _make_centers(rng: random.Random, n: int) -> list[list[float]]:
    """n cellules réparties dans l'anneau (±120 km), chacune avec une vitesse (km/tick)."""
    centers = []
    for _ in range(n):
        cx, cy = rng.uniform(-120, 120), rng.uniform(-120, 120)
        vx, vy = rng.uniform(-1.5, 1.5), rng.uniform(-1.5, 1.5)  # ≈ 30-80 km/h à 7 s/tick
        centers.append([cx, cy, vx, vy])
    return centers


def _tick_strikes(
    rng: random.Random, centers: list[list[float]], n_total: int,
    radius_km: float, now: float, window_s: float,
) -> list[Strike]:
    """Génère le buffer fenêtré d'un tick : n_total strokes répartis sur les cellules."""
    per = max(1, n_total // len(centers))
    strikes: list[Strike] = []
    for cx, cy, _vx, _vy in centers:
        for _ in range(per):
            dx = rng.gauss(0.0, radius_km / 2.0)
            dy = rng.gauss(0.0, radius_km / 2.0)
            x, y = cx + dx, cy + dy
            lat, lon = unproject_local(_HOME.lat, _HOME.lon, x, y)
            ts = now - rng.uniform(0.0, window_s)
            strikes.append(Strike(
                ts_unix=ts, lat=lat, lon=lon,
                distance_km=math.hypot(x, y), bearing_deg=0.0, mds=6,
            ))
    return strikes


def _run(cfg: AnalysisConfig, *, strikes: int, cells: int, ticks: int,
         radius_km: float, seed: int) -> dict:
    rng = random.Random(seed)
    centers = _make_centers(rng, cells)
    window_s = cfg.cell_window_minutes * 60.0
    now = 1_000_000.0
    prev: dict[int, Cell] = {}
    next_id = 1
    durations: list[float] = []
    last_cells = 0
    grid_used = strikes >= cfg.micro_cluster_min_points and cfg.micro_grid_km > 0

    for _ in range(ticks):
        batch = _tick_strikes(rng, centers, strikes, radius_km, now, window_s)
        t0 = time.perf_counter()
        result, next_id = update_cells(_HOME, batch, prev, cfg, now=now, next_id=next_id)
        durations.append((time.perf_counter() - t0) * 1000.0)
        prev = result
        last_cells = len(result)
        for c in centers:                 # avance les cellules
            c[0] += c[2]
            c[1] += c[3]
        now += cfg.tick_seconds

    return {
        "ticks": ticks, "strikes_per_tick": strikes, "cells_detected": last_cells,
        "grid_path": grid_used,
        "mean_ms": sum(durations) / len(durations),
        "median_ms": _percentile(durations, 50),
        "p95_ms": _percentile(durations, 95),
        "max_ms": max(durations), "min_ms": min(durations),
    }


def run_bench(*, strikes: int = 15_000, cells: int = 8, ticks: int = 12,
              radius_km: float = 12.0, seed: int = 1234, compare: bool = False) -> int:
    base = AnalysisConfig()
    print("StormCell - banc d'essai du pipeline d'analyse")
    print(f"   {strikes} strokes/tick | {cells} cellules | {ticks} ticks | "
          f"algo={base.cluster_algo}\n")

    on = _run(base, strikes=strikes, cells=cells, ticks=ticks, radius_km=radius_km, seed=seed)
    _print_row("grille ON " if on["grid_path"] else "par-impact", on)

    if compare:
        off_cfg = replace(base, micro_cluster_min_points=10**9)  # désactive la pré-agrégation
        off = _run(off_cfg, strikes=strikes, cells=cells, ticks=ticks, radius_km=radius_km, seed=seed)
        _print_row("grille OFF", off)
        if off["median_ms"] > 0:
            print(f"\n   -> acceleration mediane grille : x{off['median_ms'] / max(on['median_ms'], 1e-6):.2f}")
    return 0


def _print_row(label: str, r: dict) -> None:
    print(f"   [{label:<11}] mediane {r['median_ms']:7.1f} ms | "
          f"p95 {r['p95_ms']:7.1f} ms | max {r['max_ms']:7.1f} ms | "
          f"moy {r['mean_ms']:7.1f} ms | {r['cells_detected']} cellules")
