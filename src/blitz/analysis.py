"""Détection et suivi des cellules orageuses.

Améliorations clés :
- DBSCAN en métrique haversine native (cohérent jusqu'aux pôles, valide à n'importe
  quelle distance de HOME — fini la projection locale qui se déforme au-delà de 300 km)
- Filtrage qualité (MDS) + sous-échantillonnage pour rester rapide
- Tracking par plus-proche-centroïde en distance grand-cercle
- Persistance fade-out : une cellule survit `max_track_misses` ticks sans détection
- **Filtre de Kalman à vitesse constante** (repère local fixé à la naissance de la
  cellule) → vitesse/cap lissés + covariance → cône d'incertitude principiel
- **ETA au bord de cellule** : temps avant que la foudre entre dans l'anneau d'alerte
- **Nowcast probabiliste** : proba de toucher HOME dans l'horizon (covariance KF)
- **Lightning jump** : flambée du taux d'éclairs (signal orage sévère)
- **Indice de sévérité** 0..5 et **lignée split/merge** des cellules
- Tendances d'intensité et de rayon + score de confiance par cellule
"""

from __future__ import annotations

import logging
import math
import random
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable

import numpy as np
from scipy.optimize import linear_sum_assignment
from sklearn.cluster import DBSCAN

from .config import AnalysisConfig, HomeConfig
from .geo import EARTH_RADIUS_KM, haversine, project_local
from .state import Cell, Strike
from .tracking import CVKalman, lightning_jump, severity_index, strike_probability

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
    flash_rate_per_min: float


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


def _count_flashes(strikes: list[Strike], dt_s: float, dd_km: float) -> int:
    """Regroupe des *strokes* en *flashs* (proches en temps ET en espace).

    Blitzortung émet des strokes ; un éclair (flash) en compte souvent plusieurs.
    Le taux d'éclairs/min (base du lightning jump) doit se calculer en flashs.

    Fenêtre glissante : seuls les flashs « actifs » (dernier stroke dans `dt_s`)
    restent candidats — les plus vieux ne peuvent plus matcher (strokes triés par
    temps). Coût O(n·k) avec k = flashs actifs (petit) au lieu de O(n²), à résultat
    strictement identique à la version naïve.
    """
    flash_count = 0
    active: list[list[float]] = []  # [t, lat, lon] des flashs encore matchables
    for s in sorted(strikes, key=lambda x: x.ts_unix):
        cutoff = s.ts_unix - dt_s
        if active:
            active = [f for f in active if f[0] >= cutoff]  # purge des flashs expirés
        joined = False
        for f in active:
            if haversine(f[1], f[2], s.lat, s.lon) <= dd_km:
                f[0], f[1], f[2] = s.ts_unix, s.lat, s.lon  # le flash « suit » le stroke
                joined = True
                break
        if not joined:
            active.append([s.ts_unix, s.lat, s.lon])
            flash_count += 1
    return flash_count


# ─── DBSCAN haversine ───────────────────────────────────────────────────────
def _groups_from_labels(strikes: list[Strike], labels) -> list[list[Strike]]:
    """Regroupe les impacts par label DBSCAN/HDBSCAN (le bruit -1 est ignoré)."""
    groups: dict[int, list[Strike]] = defaultdict(list)
    for i, lbl in enumerate(labels):
        if lbl != -1:
            groups[int(lbl)].append(strikes[i])
    return list(groups.values())


def _label_per_strike(strikes: list[Strike], cfg: AnalysisConfig) -> list[list[Strike]]:
    """Clustering classique : un point par impact (chemin par défaut, exact)."""
    coords_rad = np.asarray([(math.radians(s.lat), math.radians(s.lon)) for s in strikes], dtype=float)
    if cfg.cluster_algo == "hdbscan":
        # HDBSCAN : densité variable → sépare mieux cellules d'une même ligne d'orage.
        from sklearn.cluster import HDBSCAN
        labels = HDBSCAN(
            min_cluster_size=max(2, cfg.hdbscan_min_cluster_size),
            metric="haversine",
            copy=True,
        ).fit_predict(coords_rad)
    else:
        eps_rad = cfg.cluster_eps_km / EARTH_RADIUS_KM
        labels = DBSCAN(
            eps=eps_rad, min_samples=cfg.cluster_min_samples,
            metric="haversine", algorithm="ball_tree",
        ).fit_predict(coords_rad)
    return _groups_from_labels(strikes, labels)


def _label_grid_aggregated(strikes: list[Strike], cfg: AnalysisConfig) -> list[list[Strike]]:
    """Pré-agrégation grille puis DBSCAN *pondéré* sur les représentants (Lot D #18).

    On regroupe d'abord les strokes dans des cellules de grille fines (≪ eps), puis
    on clusterise un représentant par cellule en passant le nombre de strokes en
    `sample_weight`. Comme la somme des poids dans un voisinage eps reproduit le
    nombre de strokes sous-jacents, la détermination des points-cœur (et donc des
    clusters) est équivalente au DBSCAN par-impact, mais sur bien moins de points.
    """
    dstep = cfg.micro_grid_km / 111.0  # pas de grille en degrés (≈ micro_grid_km)
    bins: dict[tuple[int, int], list[Strike]] = defaultdict(list)
    for s in strikes:
        bins[(round(s.lat / dstep), round(s.lon / dstep))].append(s)

    cells = list(bins.values())
    reps = np.empty((len(cells), 2), dtype=float)
    weights = np.empty(len(cells), dtype=float)
    for i, members in enumerate(cells):
        reps[i, 0] = math.radians(sum(s.lat for s in members) / len(members))
        reps[i, 1] = math.radians(sum(s.lon for s in members) / len(members))
        weights[i] = len(members)

    eps_rad = cfg.cluster_eps_km / EARTH_RADIUS_KM
    labels = DBSCAN(
        eps=eps_rad, min_samples=cfg.cluster_min_samples,
        metric="haversine", algorithm="ball_tree",
    ).fit_predict(reps, sample_weight=weights)

    groups: dict[int, list[Strike]] = defaultdict(list)
    for cell_idx, lbl in enumerate(labels):
        if lbl != -1:
            groups[int(lbl)].extend(cells[cell_idx])
    return list(groups.values())


def _detect_clusters(strikes: list[Strike], cfg: AnalysisConfig) -> list[_RawCluster]:
    """DBSCAN en métrique haversine native, valide partout sur le globe.

    Sous forte charge (≥ `micro_cluster_min_points`), une pré-agrégation grille
    réduit le nombre de points fournis à DBSCAN (cf. `_label_grid_aggregated`).
    """
    if len(strikes) < cfg.cluster_min_samples:
        return []

    if (cfg.cluster_algo != "hdbscan"
            and cfg.micro_grid_km > 0
            and len(strikes) >= cfg.micro_cluster_min_points):
        member_groups = _label_grid_aggregated(strikes, cfg)
    else:
        member_groups = _label_per_strike(strikes, cfg)

    jump_window_s = cfg.jump_window_min * 60.0
    clusters: list[_RawCluster] = []
    for members in member_groups:
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
        # Taux d'éclairs instantané : flashs (strokes regroupés) dans la fenêtre courte.
        recent = [s for s in members if s.ts_unix >= last - jump_window_s]
        n_flashes = _count_flashes(recent, cfg.flash_dt_s, cfg.flash_dd_km)
        flash_rate = n_flashes / max(cfg.jump_window_min, _EPS)
        clusters.append(
            _RawCluster(
                centroid_lat=c_lat,
                centroid_lon=c_lon,
                radius_km=radius,
                strikes_count=len(members),
                first_seen=first,
                last_seen=last,
                intensity_per_min=len(members) / duration_min,
                flash_rate_per_min=flash_rate,
            )
        )
    return clusters


# ─── Association inter-tick ─────────────────────────────────────────────────
def _associate(raws: list[_RawCluster], previous: dict[int, Cell], cfg: AnalysisConfig) -> dict[int, int]:
    """Association *optimale* clusters ↔ cellules (algorithme hongrois) avec gating
    adaptatif : la tolérance de saut croît avec la vitesse de la cellule (cellules
    rapides → on autorise un déplacement plus grand). Renvoie {index_cluster: prev_id}.

    Le coût global minimisé évite les inversions d'identifiants du greedy nearest-neighbor
    quand plusieurs cellules sont proches (recommandation SCIT).
    """
    prev_items = list(previous.items())
    if not raws or not prev_items:
        return {}
    base_gate = 2.0 * cfg.cluster_eps_km
    big = 1e7

    # Matrice de coûts (n_raws × n_prev) en haversine vectorisé (broadcast NumPy),
    # au lieu d'une double boucle Python avec un appel scalaire par paire.
    r_lat = np.radians([r.centroid_lat for r in raws])
    r_lon = np.radians([r.centroid_lon for r in raws])
    r_last = np.asarray([r.last_seen for r in raws], dtype=float)
    p_lat = np.radians([c.centroid_lat for _pid, c in prev_items])
    p_lon = np.radians([c.centroid_lon for _pid, c in prev_items])
    p_last = np.asarray([c.last_seen for _pid, c in prev_items], dtype=float)
    p_vel = np.asarray([(c.velocity_kmh or 0.0) for _pid, c in prev_items], dtype=float)

    dphi = p_lat[None, :] - r_lat[:, None]
    dlmb = p_lon[None, :] - r_lon[:, None]
    a = np.sin(dphi / 2) ** 2 + np.cos(r_lat[:, None]) * np.cos(p_lat[None, :]) * np.sin(dlmb / 2) ** 2
    d = 2.0 * EARTH_RADIUS_KM * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))

    dt = np.clip(r_last[:, None] - p_last[None, :], 0.0, None)
    gate = base_gate + (p_vel[None, :] / 3600.0) * dt   # déplacement plausible sur dt
    cost = np.where(d <= gate, d, big)

    rows, cols = linear_sum_assignment(cost)
    out: dict[int, int] = {}
    for i, j in zip(rows, cols, strict=True):
        if cost[i, j] < big:
            out[int(i)] = prev_items[j][0]
    return out


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


# ─── Mouvement & prédiction (filtre de Kalman) ──────────────────────────────
def _eta_to_radius(
    cx: float, cy: float, vx: float, vy: float, hx: float, hy: float, r_eff: float
) -> float | None:
    """Temps (minutes) avant que la cellule (cx,cy)+t·(vx,vy) entre dans le disque
    de rayon `r_eff` centré sur HOME (hx,hy). None si elle ne l'atteint jamais."""
    dx0, dy0 = cx - hx, cy - hy
    a = vx * vx + vy * vy
    if a < 1e-12:
        return None
    b = 2.0 * (dx0 * vx + dy0 * vy)
    c = dx0 * dx0 + dy0 * dy0 - r_eff * r_eff
    if c <= 0:
        return 0.0  # HOME déjà sous la foudre
    disc = b * b - 4 * a * c
    if disc < 0:
        return None
    sq = math.sqrt(disc)
    roots = [r for r in ((-b - sq) / (2 * a), (-b + sq) / (2 * a)) if r >= 0]
    if not roots:
        return None
    return min(roots) / 60.0


def _apply_kalman(cell: Cell, prev: Cell | None, home: HomeConfig, cfg: AnalysisConfig) -> None:
    """Met à jour le filtre de Kalman de la cellule, puis dérive vitesse/cap/ETA/proba.

    Le repère plan local est fixé à la naissance de la cellule (`ref_lat/ref_lon`),
    pour que l'état du filtre reste continu d'un tick à l'autre.
    """
    meas_var = cfg.kf_meas_noise_km ** 2
    accel_var = cfg.kf_process_accel ** 2

    if prev is not None and prev.kf_x is not None and prev.ref_lat is not None:
        ref_lat, ref_lon = prev.ref_lat, prev.ref_lon
        kf = CVKalman.from_state(prev.kf_x, prev.kf_p, meas_var, accel_var)
        dt = cell.last_seen - prev.last_seen
        if dt > 0:
            kf.predict(dt)
        zx, zy = project_local(ref_lat, ref_lon, cell.centroid_lat, cell.centroid_lon)
        kf.update(zx, zy)
        updates = prev.kf_updates + 1
    else:
        ref_lat, ref_lon = cell.centroid_lat, cell.centroid_lon
        kf = CVKalman.init(0.0, 0.0, meas_var, accel_var)
        updates = 1

    cell.ref_lat, cell.ref_lon = ref_lat, ref_lon
    cell.kf_x = kf.state_list()
    cell.kf_p = kf.cov_list()
    cell.kf_updates = updates

    if updates < 2:
        return  # pas encore de vitesse fiable

    vx, vy = kf.vel
    speed_kms = kf.speed_kms()
    if speed_kms < 1e-5:
        return
    cell.velocity_kmh = speed_kms * 3600.0
    cell.heading_deg = (math.degrees(math.atan2(vx, vy)) + 360.0) % 360.0

    d_home = haversine(home.lat, home.lon, cell.centroid_lat, cell.centroid_lon)
    if d_home > cfg.eta_max_radius_km:
        cell.closest_approach_km = d_home
        return

    cx, cy = kf.pos
    hx, hy = project_local(ref_lat, ref_lon, home.lat, home.lon)
    to_hx, to_hy = hx - cx, hy - cy
    v2 = vx * vx + vy * vy
    t_closest = (to_hx * vx + to_hy * vy) / v2  # secondes
    pos_std = kf.pos_std_km()

    if t_closest <= 0:
        # la cellule s'éloigne : pas d'ETA, on garde la distance courante
        cell.closest_approach_km = math.hypot(to_hx, to_hy)
        cell.eta_minutes = None
        cell.eta_strike_minutes = None
        cell.eta_uncertainty_min = None
        cell.strike_probability = 0.0
        return

    clx, cly = cx + vx * t_closest, cy + vy * t_closest
    closest = math.hypot(hx - clx, hy - cly)
    cell.eta_minutes = t_closest / 60.0
    cell.closest_approach_km = closest
    cell.eta_uncertainty_min = (pos_std / (speed_kms + _EPS)) / 60.0

    # ETA au bord : foudre dans l'anneau (rayon de cellule + anneau d'alerte)
    r_eff = cell.radius_km + cfg.strike_ring_km
    cell.eta_strike_minutes = _eta_to_radius(cx, cy, vx, vy, hx, hy, r_eff)

    # nowcast probabiliste
    cell.strike_probability = strike_probability(
        closest_approach_km=closest,
        eta_minutes=cell.eta_minutes,
        pos_std_km=pos_std,
        effective_radius_km=r_eff,
        horizon_min=cfg.nowcast_horizon_min,
    )


def _confidence(cell: Cell, n_track: int, has_motion: bool) -> float:
    """Score 0..1 basé sur la longueur du track, le nb d'impacts et la stabilité."""
    if not has_motion:
        return 0.0
    track_score = min(n_track / 6.0, 1.0)      # 6 ticks ≈ saturation
    size_score = min(cell.strikes_count / 30.0, 1.0)
    persist_penalty = 1.0 - min(cell.misses / 3.0, 1.0)
    return round(0.5 * track_score + 0.3 * size_score + 0.2 * persist_penalty, 3)


# ─── Lignée (split / merge) ─────────────────────────────────────────────────
def _resolve_lineage(
    new_cells: dict[int, Cell],
    matched_ids: set[int],
    previous: dict[int, Cell],
    cfg: AnalysisConfig,
) -> set[int]:
    """Étiquette splits (parent_id) et merges (merged_from). Renvoie les ids
    précédents absorbés par un merge (à ne pas faire survivre en fantôme)."""
    merged_ids: set[int] = set()

    for cell in new_cells.values():
        # Merge : d'autres cellules précédentes non rematchées sont sous le rayon courant
        if cell.cell_id in matched_ids:
            gate = max(cell.radius_km, cfg.cluster_eps_km)
            for pid, pcell in previous.items():
                if pid == cell.cell_id or pid in matched_ids:
                    continue
                if haversine(cell.centroid_lat, cell.centroid_lon, pcell.centroid_lat, pcell.centroid_lon) <= gate:
                    cell.merged_from.append(pid)
                    merged_ids.add(pid)
        # Split : une nouvelle cellule (id neuf) née à côté d'une cellule mère rematchée
        else:
            best_parent, best_d = None, 2.0 * cfg.cluster_eps_km
            for pid in matched_ids:
                pcell = previous.get(pid)
                if pcell is None:
                    continue
                d = haversine(cell.centroid_lat, cell.centroid_lon, pcell.centroid_lat, pcell.centroid_lon)
                if d < best_d:
                    best_d, best_parent = d, pid
            if best_parent is not None:
                cell.parent_id = best_parent

    return merged_ids


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
    4. Filtre de Kalman → mouvement, ETA (centroïde + bord), proba, incertitude
    5. Tendances, lightning jump, sévérité, confiance, lignée split/merge
    6. Persistance fade-out des cellules non matchées
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

    assignment = _associate(raw_clusters, previous, cfg)   # index cluster → prev_id (optimal)
    used: set[int] = set(assignment.values())
    new_cells: dict[int, Cell] = {}

    for idx, raw in enumerate(raw_clusters):
        prev_id = assignment.get(idx)
        prev_cell: Cell | None = previous.get(prev_id) if prev_id is not None else None
        if prev_id is None:
            cid = next_id
            next_id += 1
            track: list[tuple[float, float, float]] = []
            r_hist: list[tuple[float, float]] = []
            i_hist: list[tuple[float, float]] = []
        else:
            cid = prev_id
            track = list(prev_cell.track)
            r_hist = list(prev_cell.radius_history)
            i_hist = list(prev_cell.intensity_history)

        if not track or raw.last_seen > track[-1][0]:
            track.append((raw.last_seen, raw.centroid_lat, raw.centroid_lon))
            r_hist.append((raw.last_seen, raw.radius_km))
            # Tendance/jump basés sur le taux d'éclairs instantané (plus réactif que
            # la moyenne sur la vie de la cellule).
            i_hist.append((raw.last_seen, float(raw.flash_rate_per_min)))
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
            flash_rate_per_min=raw.flash_rate_per_min,
            first_seen=raw.first_seen,
            last_seen=raw.last_seen,
            track=track,
            radius_history=r_hist,
            intensity_history=i_hist,
            misses=0,
        )
        _apply_kalman(cell, prev_cell, home, cfg)
        cell.intensity_trend = _trend(i_hist)
        cell.radius_trend = _trend(r_hist)
        cell.jump_detected, _ = lightning_jump(
            i_hist,
            sigma_mult=cfg.jump_sigma_mult,
            min_rate=cfg.jump_min_flash_rate,
            lookback_s=cfg.jump_dfrdt_lookback_s,
            sigma_window_s=cfg.jump_sigma_window_s,
        )
        cell.severity = severity_index(
            cell.flash_rate_per_min, cell.radius_km, cell.strikes_count,
            cell.intensity_trend, cell.jump_detected,
        )
        cell.confidence = _confidence(cell, len(track), cell.velocity_kmh is not None)
        new_cells[cid] = cell

    # Lignée split/merge (avant la persistance fade-out)
    merged_ids = _resolve_lineage(new_cells, set(used), previous, cfg)

    # Persistance fade-out : on garde quelques ticks les cellules disparues du DBSCAN
    for old_id, old_cell in previous.items():
        if old_id in new_cells or old_id in used or old_id in merged_ids:
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
            flash_rate_per_min=old_cell.flash_rate_per_min,
            first_seen=old_cell.first_seen,
            last_seen=old_cell.last_seen,
            track=old_cell.track,
            radius_history=old_cell.radius_history,
            intensity_history=old_cell.intensity_history,
            velocity_kmh=old_cell.velocity_kmh,
            heading_deg=old_cell.heading_deg,
            eta_minutes=old_cell.eta_minutes,
            eta_strike_minutes=old_cell.eta_strike_minutes,
            eta_uncertainty_min=old_cell.eta_uncertainty_min,
            closest_approach_km=old_cell.closest_approach_km,
            strike_probability=old_cell.strike_probability,
            intensity_trend=old_cell.intensity_trend,
            radius_trend=old_cell.radius_trend,
            jump_detected=old_cell.jump_detected,
            severity=old_cell.severity,
            confidence=max(0.0, old_cell.confidence - 0.25),
            misses=old_cell.misses + 1,
            parent_id=old_cell.parent_id,
            merged_from=old_cell.merged_from,
            ref_lat=old_cell.ref_lat,
            ref_lon=old_cell.ref_lon,
            kf_x=old_cell.kf_x,
            kf_p=old_cell.kf_p,
            kf_updates=old_cell.kf_updates,
        )
        new_cells[old_id] = ghost

    return new_cells, next_id


def filter_window(strikes: Iterable[Strike], window_minutes: float, now: float | None = None) -> list[Strike]:
    """Garde les impacts dont ts_unix ∈ [now - window, now]."""
    if now is None:
        now = time.time()
    cutoff = now - window_minutes * 60.0
    return [s for s in strikes if s.ts_unix >= cutoff]
