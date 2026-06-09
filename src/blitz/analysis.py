"""Détection et suivi des cellules orageuses.

Améliorations clés :
- DBSCAN en métrique haversine native (cohérent jusqu'aux pôles, valide à n'importe
  quelle distance de HOME — fini la projection locale qui se déforme au-delà de 300 km)
- Filtrage qualité (MDS) + sous-échantillonnage stratifié pour rester rapide
- Tracking par plus-proche-centroïde en distance grand-cercle
- Persistance fade-out : une cellule survit `max_track_misses` ticks sans détection
- Régression de mouvement en projection locale **autour du centroïde courant**
  (et non autour de HOME — c'est ce qui rend valide la prédiction à grande distance)
- Cône d'incertitude (variance résiduelle de la régression → marge ETA)
- Tendances d'intensité et de rayon (croissance/déclin)
- Score de confiance par cellule
"""

from __future__ import annotations

import logging
import math
import random
import time
from dataclasses import dataclass
from typing import Iterable

import numpy as np
from sklearn.cluster import DBSCAN

from .config import AnalysisConfig, HomeConfig
from .geo import EARTH_RADIUS_KM, haversine, project_local, unproject_local
from .state import Cell, Strike

logger = logging.getLogger(__name__)

# Petit utilitaire numérique
_EPS = 1e-9


# ─── Données intermédiaires ─────────────────────────────────────────────────
@dataclass
class _RawCluster:
    centroid_lat: float
    centroid_lon: float
    radius_km: float
    strikes_count: int
    first_seen: float
    last_seen: float
    intensity_per_min: float


# ─── Pré-traitement ─────────────────────────────────────────────────────────
def _filter_quality(strikes: list[Strike], min_mds: int) -> list[Strike]:
    """Drop les impacts avec un mds connu mais en dessous du seuil."""
    if min_mds <= 0:
        return strikes
    return [s for s in strikes if s.mds is None or s.mds >= min_mds]


def _subsample(strikes: list[Strike], cap: int) -> list[Strike]:
    """Sous-échantillonnage uniforme si la liste dépasse `cap`. Déterministe via seed."""
    if len(strikes) <= cap:
        return strikes
    rng = random.Random(12345)
    return rng.sample(strikes, cap)


# ─── DBSCAN haversine ───────────────────────────────────────────────────────
def _detect_clusters(strikes: list[Strike], cfg: AnalysisConfig) -> list[_RawCluster]:
    """DBSCAN en métrique haversine native, valide partout sur le globe."""
    if len(strikes) < cfg.cluster_min_samples:
        return []
    coords_rad = np.asarray([(math.radians(s.lat), math.radians(s.lon)) for s in strikes], dtype=float)
    eps_rad = cfg.cluster_eps_km / EARTH_RADIUS_KM
    labels = DBSCAN(
        eps=eps_rad,
        min_samples=cfg.cluster_min_samples,
        metric="haversine",
        algorithm="ball_tree",
    ).fit_predict(coords_rad)

    clusters: list[_RawCluster] = []
    for lbl in sorted(set(labels)):
        if lbl == -1:
            continue
        mask = labels == lbl
        members = [strikes[i] for i, m in enumerate(mask) if m]
        # Centroïde en moyenne pondérée des coords sphériques (xyz) puis reprojection,
        # plus robuste qu'une moyenne arithmétique des lat/lon (qui faute au méridien).
        lats = np.asarray([math.radians(s.lat) for s in members])
        lons = np.asarray([math.radians(s.lon) for s in members])
        x = (np.cos(lats) * np.cos(lons)).mean()
        y = (np.cos(lats) * np.sin(lons)).mean()
        z = np.sin(lats).mean()
        hyp = math.hypot(x, y)
        c_lat = math.degrees(math.atan2(z, hyp))
        c_lon = math.degrees(math.atan2(y, x))

        radius = max((haversine(c_lat, c_lon, s.lat, s.lon) for s in members), default=0.0)
        first = min(s.ts_unix for s in members)
        last = max(s.ts_unix for s in members)
        duration_min = max((last - first) / 60.0, 1 / 60.0)
        clusters.append(
            _RawCluster(
                centroid_lat=c_lat,
                centroid_lon=c_lon,
                radius_km=radius,
                strikes_count=len(members),
                first_seen=first,
                last_seen=last,
                intensity_per_min=len(members) / duration_min,
            )
        )
    return clusters


# ─── Association inter-tick ─────────────────────────────────────────────────
def _match_previous(
    raw: _RawCluster,
    previous: dict[int, Cell],
    max_jump_km: float,
    used: set[int],
) -> int | None:
    """Greedy nearest-neighbor sur distance haversine. Un id ne peut être réutilisé qu'une fois."""
    best_id: int | None = None
    best_d = max_jump_km
    for cid, cell in previous.items():
        if cid in used:
            continue
        d = haversine(raw.centroid_lat, raw.centroid_lon, cell.centroid_lat, cell.centroid_lon)
        if d < best_d:
            best_d = d
            best_id = cid
    return best_id


# ─── Tendances simples ──────────────────────────────────────────────────────
def _trend(history: list[tuple[float, float]], min_points: int = 3, rel_thresh: float = 0.10) -> str | None:
    """Renvoie 'growing' / 'stable' / 'declining' selon la pente normalisée sur min_points récents."""
    if len(history) < min_points:
        return None
    pts = history[-max(min_points, 6):]
    t = np.asarray([p[0] for p in pts], dtype=float)
    v = np.asarray([p[1] for p in pts], dtype=float)
    if t[-1] - t[0] < 30 or v.mean() < _EPS:
        return None
    slope, _ = np.polyfit(t, v, 1)            # unités / s
    horizon_s = max(t[-1] - t[0], 60.0)
    rel = (slope * horizon_s) / max(v.mean(), _EPS)  # variation relative sur la fenêtre
    if rel > rel_thresh:
        return "growing"
    if rel < -rel_thresh:
        return "declining"
    return "stable"


# ─── Mouvement & prédiction ─────────────────────────────────────────────────
def _compute_motion(cell: Cell, home: HomeConfig, cfg: AnalysisConfig) -> None:
    """Régression linéaire en plan local **autour du centroïde courant**.

    Cette projection locale est valide tant que la cellule ne s'est pas déplacée de
    plus de quelques centaines de km dans la fenêtre de régression — ce qui est le
    cas concret pour des orages (15-100 km/h sur 15 min = max ~25 km).
    """
    if len(cell.track) < 3:
        return

    cutoff = cell.last_seen - cfg.motion_history_seconds
    pts = [p for p in cell.track if p[0] >= cutoff]
    if len(pts) < 3:
        pts = cell.track[-3:]
    if pts[-1][0] - pts[0][0] < 30:
        return  # moins de 30 s d'historique : ne pas se prononcer

    # Projection locale autour du centroïde courant
    ref_lat, ref_lon = cell.centroid_lat, cell.centroid_lon
    t_arr = np.asarray([p[0] for p in pts], dtype=float)
    xy = np.asarray([project_local(ref_lat, ref_lon, p[1], p[2]) for p in pts], dtype=float)
    t0 = t_arr[0]
    dt = t_arr - t0

    vx, x0_fit = np.polyfit(dt, xy[:, 0], 1)
    vy, y0_fit = np.polyfit(dt, xy[:, 1], 1)
    # Résidus quadratiques moyens (km) → bruit du modèle de mouvement
    res_x = xy[:, 0] - (vx * dt + x0_fit)
    res_y = xy[:, 1] - (vy * dt + y0_fit)
    rms_km = math.sqrt(float(np.mean(res_x ** 2 + res_y ** 2)))

    speed_kms = math.hypot(vx, vy)
    if speed_kms < 1e-5:
        return
    cell.velocity_kmh = speed_kms * 3600.0
    # heading : x = est, y = nord → bearing = atan2(est, nord)
    heading = (math.degrees(math.atan2(vx, vy)) + 360.0) % 360.0
    cell.heading_deg = heading

    # ETA HOME — uniquement si la cellule n'est pas trop loin (au-delà, ça n'a pas de sens
    # météo de prédire son arrivée — elle évoluera bien avant).
    d_home = haversine(home.lat, home.lon, ref_lat, ref_lon)
    if d_home > cfg.eta_max_radius_km:
        cell.closest_approach_km = d_home
        cell.eta_minutes = None
        cell.eta_uncertainty_min = None
        return

    # HOME dans le repère local centré sur (ref_lat, ref_lon)
    hx, hy = project_local(ref_lat, ref_lon, home.lat, home.lon)
    # Position courante de la cellule dans ce même repère (proche de l'origine, dérivée du fit en dt=dt[-1])
    cx = vx * dt[-1] + x0_fit
    cy = vy * dt[-1] + y0_fit
    # Vecteur cellule → HOME
    to_home_x = hx - cx
    to_home_y = hy - cy
    v_norm2 = vx * vx + vy * vy
    t_closest = (to_home_x * vx + to_home_y * vy) / v_norm2  # secondes
    if t_closest <= 0:
        cell.eta_minutes = None
        cell.closest_approach_km = math.hypot(hx - cx, hy - cy)
        cell.eta_uncertainty_min = None
        return

    closest_x = cx + vx * t_closest
    closest_y = cy + vy * t_closest
    closest = math.hypot(hx - closest_x, hy - closest_y)
    cell.eta_minutes = t_closest / 60.0
    cell.closest_approach_km = closest
    # Incertitude ETA : marge temporelle ≈ rms_residual / speed
    cell.eta_uncertainty_min = (rms_km / (speed_kms + _EPS)) / 60.0


def _confidence(cell: Cell, n_track: int, has_motion: bool) -> float:
    """Score 0..1 basé sur la longueur du track, le nb d'impacts et la stabilité."""
    if not has_motion:
        return 0.0
    track_score = min(n_track / 6.0, 1.0)      # 6 ticks ≈ saturation
    size_score = min(cell.strikes_count / 30.0, 1.0)
    persist_penalty = 1.0 - min(cell.misses / 3.0, 1.0)
    return round(0.5 * track_score + 0.3 * size_score + 0.2 * persist_penalty, 3)


# ─── Pipeline principal ─────────────────────────────────────────────────────
def update_cells(
    home: HomeConfig,
    strikes: list[Strike],
    previous: dict[int, Cell],
    cfg: AnalysisConfig,
    now: float | None = None,
    next_id: int = 1,
) -> tuple[dict[int, Cell], int]:
    """Recalcul des cellules pour le tick courant.

    Étapes :
    1. Filtre qualité (MDS) + sous-échantillonnage si trop d'impacts
    2. DBSCAN haversine
    3. Association avec les cellules du tick précédent (greedy nearest centroid)
    4. Calcul du mouvement, des tendances, du score de confiance
    5. Persistance fade-out des cellules non matchées
    """
    if now is None:
        now = time.time()

    pool = _filter_quality(strikes, cfg.min_mds_quality)
    if cfg.max_strikes_for_clustering and len(pool) > cfg.max_strikes_for_clustering:
        logger.info(
            "Sous-échantillonnage clustering : %d → %d impacts",
            len(pool), cfg.max_strikes_for_clustering,
        )
        pool = _subsample(pool, cfg.max_strikes_for_clustering)

    raw_clusters = _detect_clusters(pool, cfg)

    new_cells: dict[int, Cell] = {}
    used: set[int] = set()
    max_jump = 2.0 * cfg.cluster_eps_km

    for raw in raw_clusters:
        prev_id = _match_previous(raw, previous, max_jump, used)
        if prev_id is None:
            cid = next_id
            next_id += 1
            track: list[tuple[float, float, float]] = []
            r_hist: list[tuple[float, float]] = []
            i_hist: list[tuple[float, float]] = []
        else:
            used.add(prev_id)
            cid = prev_id
            track = list(previous[prev_id].track)
            r_hist = list(previous[prev_id].radius_history)
            i_hist = list(previous[prev_id].intensity_history)

        if not track or raw.last_seen > track[-1][0]:
            track.append((raw.last_seen, raw.centroid_lat, raw.centroid_lon))
            r_hist.append((raw.last_seen, raw.radius_km))
            # Tendance d'activité = nb d'impacts par tick (plus stable que `n/durée`,
            # cette dernière fluctuant énormément quand un cluster naît en ~1 s).
            i_hist.append((raw.last_seen, float(raw.strikes_count)))
        # bornes pour éviter la croissance non bornée
        track = track[-60:]
        r_hist = r_hist[-60:]
        i_hist = i_hist[-60:]

        cell = Cell(
            cell_id=cid,
            centroid_lat=raw.centroid_lat,
            centroid_lon=raw.centroid_lon,
            radius_km=raw.radius_km,
            strikes_count=raw.strikes_count,
            intensity_per_min=raw.intensity_per_min,
            first_seen=raw.first_seen,
            last_seen=raw.last_seen,
            track=track,
            radius_history=r_hist,
            intensity_history=i_hist,
            misses=0,
        )
        _compute_motion(cell, home, cfg)
        cell.intensity_trend = _trend(i_hist)
        cell.radius_trend = _trend(r_hist)
        cell.confidence = _confidence(cell, len(track), cell.velocity_kmh is not None)
        new_cells[cid] = cell

    # Persistance fade-out : on garde quelques ticks les cellules disparues du DBSCAN
    for old_id, old_cell in previous.items():
        if old_id in new_cells or old_id in used:
            continue
        if old_cell.misses + 1 > cfg.max_track_misses:
            continue
        ghost = Cell(
            cell_id=old_id,
            centroid_lat=old_cell.centroid_lat,
            centroid_lon=old_cell.centroid_lon,
            radius_km=old_cell.radius_km,
            strikes_count=old_cell.strikes_count,
            intensity_per_min=old_cell.intensity_per_min,
            first_seen=old_cell.first_seen,
            last_seen=old_cell.last_seen,
            track=old_cell.track,
            radius_history=old_cell.radius_history,
            intensity_history=old_cell.intensity_history,
            velocity_kmh=old_cell.velocity_kmh,
            heading_deg=old_cell.heading_deg,
            eta_minutes=old_cell.eta_minutes,
            eta_uncertainty_min=old_cell.eta_uncertainty_min,
            closest_approach_km=old_cell.closest_approach_km,
            intensity_trend=old_cell.intensity_trend,
            radius_trend=old_cell.radius_trend,
            confidence=max(0.0, old_cell.confidence - 0.25),
            misses=old_cell.misses + 1,
        )
        new_cells[old_id] = ghost

    return new_cells, next_id


def filter_window(strikes: Iterable[Strike], window_minutes: float, now: float | None = None) -> list[Strike]:
    """Garde les impacts dont ts_unix ∈ [now - window, now]."""
    if now is None:
        now = time.time()
    cutoff = now - window_minutes * 60.0
    return [s for s in strikes if s.ts_unix >= cutoff]
