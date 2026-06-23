"""Suivi avancé des cellules : filtre de Kalman + détection de *lightning jump*.

Le filtre de Kalman à vitesse quasi-constante lisse la trajectoire d'une cellule
et fournit une covariance d'état → cône d'incertitude *principiel* (au lieu d'un
RMS de régression refait à zéro chaque tick, qui saute beaucoup).

Unités internes : kilomètres et secondes. État `[x, y, vx, vy]` dans un repère
plan local (est, nord) fixé à la naissance de la cellule (cf. `geo.project_local`).
"""

from __future__ import annotations

import math
from typing import Sequence

import numpy as np

_SQRT2 = math.sqrt(2.0)


def _normal_cdf(z: float) -> float:
    """P(N(0,1) <= z) via erf."""
    return 0.5 * (1.0 + math.erf(z / _SQRT2))


class CVKalman:
    """Filtre de Kalman à vitesse constante (modèle bruit blanc d'accélération).

    - `meas_var`   : variance de la mesure de position (km²), bruit du centroïde.
    - `accel_var`  : densité de bruit de process (accélération) (km²/s⁴).
    """

    def __init__(self, X: np.ndarray, P: np.ndarray, meas_var: float, accel_var: float) -> None:
        self.X = np.asarray(X, dtype=float).reshape(4)
        self.P = np.asarray(P, dtype=float).reshape(4, 4)
        self.meas_var = float(meas_var)
        self.accel_var = float(accel_var)

    # ── constructeurs ────────────────────────────────────────────────────────
    @classmethod
    def init(
        cls,
        x: float,
        y: float,
        meas_var: float,
        accel_var: float,
        init_vel_var: float = 1e-3,
    ) -> "CVKalman":
        P = np.diag([meas_var, meas_var, init_vel_var, init_vel_var]).astype(float)
        X = np.array([x, y, 0.0, 0.0], dtype=float)
        return cls(X, P, meas_var, accel_var)

    @classmethod
    def from_state(
        cls,
        x: Sequence[float],
        p: Sequence[Sequence[float]],
        meas_var: float,
        accel_var: float,
    ) -> "CVKalman":
        return cls(np.asarray(x, dtype=float), np.asarray(p, dtype=float), meas_var, accel_var)

    # ── cycle predict / update ───────────────────────────────────────────────
    def predict(self, dt: float) -> None:
        dt = float(max(dt, 1e-3))
        F = np.array(
            [[1, 0, dt, 0],
             [0, 1, 0, dt],
             [0, 0, 1, 0],
             [0, 0, 0, 1]],
            dtype=float,
        )
        q = self.accel_var
        dt2 = dt * dt
        dt3 = dt2 * dt
        dt4 = dt3 * dt
        Q = q * np.array(
            [[dt4 / 4, 0, dt3 / 2, 0],
             [0, dt4 / 4, 0, dt3 / 2],
             [dt3 / 2, 0, dt2, 0],
             [0, dt3 / 2, 0, dt2]],
            dtype=float,
        )
        self.X = F @ self.X
        self.P = F @ self.P @ F.T + Q

    def update(self, zx: float, zy: float) -> None:
        H = np.array([[1, 0, 0, 0], [0, 1, 0, 0]], dtype=float)
        R = np.diag([self.meas_var, self.meas_var]).astype(float)
        z = np.array([zx, zy], dtype=float)
        y = z - H @ self.X
        S = H @ self.P @ H.T + R
        K = self.P @ H.T @ np.linalg.inv(S)
        self.X = self.X + K @ y
        self.P = (np.eye(4) - K @ H) @ self.P

    # ── accès ────────────────────────────────────────────────────────────────
    @property
    def pos(self) -> tuple[float, float]:
        return float(self.X[0]), float(self.X[1])

    @property
    def vel(self) -> tuple[float, float]:
        return float(self.X[2]), float(self.X[3])

    def speed_kms(self) -> float:
        return float(math.hypot(self.X[2], self.X[3]))

    def pos_std_km(self) -> float:
        """Écart-type de position (km), moyenné sur x/y."""
        return float(math.sqrt(max(0.5 * (self.P[0, 0] + self.P[1, 1]), 0.0)))

    def state_list(self) -> list[float]:
        return [float(v) for v in self.X]

    def cov_list(self) -> list[list[float]]:
        return [[float(v) for v in row] for row in self.P]


# ─── Lightning jump (signal orage sévère) ────────────────────────────────────
def lightning_jump(
    history: Sequence[tuple[float, float]],
    sigma_mult: float = 2.0,
    min_points: int = 6,
    min_rate: float = 10.0,
    lookback_s: float = 120.0,
    sigma_window_s: float = 720.0,
) -> tuple[bool, float]:
    """Détecteur de *lightning jump* 2σ (d'après Schultz et al. 2009).

    `history` : série (t_unix, taux de flashs /min). On calcule la dérivée du taux
    (DFRDT) sur ~`lookback_s` (≈ 2 min), puis on la compare à `sigma_mult`×σ du bruit
    de DFRDT sur ~`sigma_window_s` (≈ 12 min). Un jump est retenu si le taux courant
    dépasse `min_rate` flashs/min ET DFRDT ≥ 2σ. Préavis typique ~20 min vers le sévère.

    Renvoie (detected, level) où level = DFRDT_courant / σ_fond.
    """
    if len(history) < min_points:
        return False, 0.0
    t = np.asarray([p[0] for p in history], dtype=float)
    r = np.asarray([p[1] for p in history], dtype=float)
    t0, t_now = float(t[0]), float(t[-1])
    look_min = lookback_s / 60.0

    def dfrdt_at(idx: int) -> float | None:
        ti = t[idx]
        tp = ti - lookback_s
        if tp < t0:
            return None
        rp = float(np.interp(tp, t, r))
        return (float(r[idx]) - rp) / look_min

    latest = dfrdt_at(len(t) - 1)
    if latest is None:
        return False, 0.0

    background = []
    for idx in range(len(t) - 1):
        if t_now - t[idx] > sigma_window_s:
            continue
        d = dfrdt_at(idx)
        if d is not None:
            background.append(d)
    if len(background) < 3:
        return False, 0.0
    sigma = float(np.std(background))
    if sigma < 1e-6:
        return False, 0.0

    level = latest / sigma
    current_rate = float(r[-1])
    detected = bool(level >= sigma_mult and latest > 0.0 and current_rate >= min_rate)
    return detected, round(level, 2)


# ─── Indice de sévérité 0..5 ─────────────────────────────────────────────────
def severity_index(
    flash_rate_per_min: float | None,
    radius_km: float,
    strikes_count: int,
    intensity_trend: str | None,
    jump_detected: bool,
) -> float:
    """Score composite 0..5 : taux d'éclairs + taille + densité + tendance + jump."""
    rate = flash_rate_per_min or 0.0
    score = min(rate / 20.0, 1.0) * 2.5            # taux (0..2.5)
    score += min(radius_km / 40.0, 1.0)            # taille (0..1)
    area = math.pi * max(radius_km, 1.0) ** 2
    density = strikes_count / area                  # éclairs/km²
    score += min(density / 0.5, 1.0) * 0.5         # densité (0..0.5)
    if intensity_trend == "growing":
        score += 0.5
    if jump_detected:
        score += 0.5
    return round(min(score, 5.0), 1)


# ─── Nowcast probabiliste ────────────────────────────────────────────────────
def strike_probability(
    closest_approach_km: float | None,
    eta_minutes: float | None,
    pos_std_km: float,
    effective_radius_km: float,
    horizon_min: float = 30.0,
) -> float:
    """Probabilité (0..1) que HOME soit touché dans `horizon_min`.

    Approche analytique : à l'approche la plus proche, la distance prédite est
    `closest_approach_km` avec incertitude `pos_std_km` (gaussienne). On considère
    un "coup" si la distance passe sous `effective_radius_km` (= rayon + anneau).
    On ne compte que si l'approche se produit dans l'horizon.
    """
    if closest_approach_km is None or eta_minutes is None:
        return 0.0
    if eta_minutes < 0 or eta_minutes > horizon_min:
        return 0.0
    sigma = max(pos_std_km, 0.5)
    z = (effective_radius_km - closest_approach_km) / sigma
    return round(max(0.0, min(1.0, _normal_cdf(z))), 3)
