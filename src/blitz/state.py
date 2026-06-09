"""État partagé entre le worker MQTT, l'analyse et le serveur web."""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class Strike:
    """Un impact reçu et conservé en mémoire (fenêtre glissante)."""
    ts_unix: float
    lat: float
    lon: float
    distance_km: float
    bearing_deg: float
    mds: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts_unix": self.ts_unix,
            "lat": self.lat,
            "lon": self.lon,
            "distance_km": self.distance_km,
            "bearing_deg": self.bearing_deg,
            "mds": self.mds,
        }


@dataclass
class Cell:
    """Cellule orageuse détectée et suivie dans le temps."""
    cell_id: int
    centroid_lat: float
    centroid_lon: float
    radius_km: float
    strikes_count: int
    intensity_per_min: float
    first_seen: float
    last_seen: float
    # Historiques (t, lat, lon) pour la régression de mouvement et les tendances
    track: list[tuple[float, float, float]] = field(default_factory=list)
    radius_history: list[tuple[float, float]] = field(default_factory=list)        # (t, radius_km)
    intensity_history: list[tuple[float, float]] = field(default_factory=list)     # (t, n_per_min)
    velocity_kmh: Optional[float] = None
    heading_deg: Optional[float] = None
    eta_minutes: Optional[float] = None
    eta_uncertainty_min: Optional[float] = None
    closest_approach_km: Optional[float] = None
    intensity_trend: Optional[str] = None     # "growing" | "stable" | "declining"
    radius_trend: Optional[str] = None        # idem
    confidence: float = 0.0                    # 0..1 (qualité du tracking/prédiction)
    misses: int = 0                            # ticks consécutifs sans détection (persistance)
    # ── prédiction avancée (Vague 2) ─────────────────────────────────────────
    flash_rate_per_min: Optional[float] = None    # taux d'éclairs instantané (fenêtre courte)
    eta_strike_minutes: Optional[float] = None    # temps avant que la foudre entre dans l'anneau d'alerte
    strike_probability: Optional[float] = None    # 0..1 : proba de toucher HOME dans l'horizon nowcast
    jump_detected: bool = False                   # flambée du taux d'éclairs (signal orage sévère)
    severity: float = 0.0                         # indice de sévérité 0..5
    parent_id: Optional[int] = None               # cellule mère si issue d'un split
    merged_from: list[int] = field(default_factory=list)  # ids fusionnés dans cette cellule
    # ── état interne du filtre de Kalman (non sérialisé) ─────────────────────
    ref_lat: Optional[float] = None               # origine du repère local (fixe sur la vie de la cellule)
    ref_lon: Optional[float] = None
    kf_x: Optional[list[float]] = None            # état [x, y, vx, vy] (km, km/s)
    kf_p: Optional[list[list[float]]] = None      # covariance 4x4
    kf_updates: int = 0                           # nb de mises à jour du filtre

    def to_dict(self) -> dict[str, Any]:
        return {
            "cell_id": self.cell_id,
            "centroid": {"lat": self.centroid_lat, "lon": self.centroid_lon},
            "radius_km": self.radius_km,
            "strikes_count": self.strikes_count,
            "intensity_per_min": self.intensity_per_min,
            "flash_rate_per_min": self.flash_rate_per_min,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "velocity_kmh": self.velocity_kmh,
            "heading_deg": self.heading_deg,
            "eta_minutes": self.eta_minutes,
            "eta_strike_minutes": self.eta_strike_minutes,
            "eta_uncertainty_min": self.eta_uncertainty_min,
            "closest_approach_km": self.closest_approach_km,
            "strike_probability": self.strike_probability,
            "intensity_trend": self.intensity_trend,
            "radius_trend": self.radius_trend,
            "jump_detected": self.jump_detected,
            "severity": round(self.severity, 1),
            "confidence": round(self.confidence, 3),
            "parent_id": self.parent_id,
            "merged_from": self.merged_from,
            "misses": self.misses,
        }


class SharedState:
    """Conteneur thread-safe pour les données live consommées par le serveur web."""

    def __init__(self, max_strikes_recent: int = 5000) -> None:
        self.lock = threading.Lock()
        self.recent: deque[Strike] = deque(maxlen=max_strikes_recent)
        self.cells: dict[int, Cell] = {}
        self.stats: dict[str, Any] = {
            "total_world": 0,
            "nearby": 0,
            "closest_km": None,
            "started_at": time.time(),
            "logged_session": 0,
            "logged_total": 0,
            "last_message_at": None,
            "mqtt_connected": False,
        }

    def add_strike(self, s: Strike) -> None:
        with self.lock:
            self.recent.append(s)
            self.stats["nearby"] += 1
            if self.stats["closest_km"] is None or s.distance_km < self.stats["closest_km"]:
                self.stats["closest_km"] = s.distance_km
            self.stats["last_message_at"] = time.time()

    def bump_world(self) -> None:
        with self.lock:
            self.stats["total_world"] += 1
            self.stats["last_message_at"] = time.time()

    def snapshot_recent_since(self, since_unix: float) -> list[Strike]:
        with self.lock:
            return [s for s in self.recent if s.ts_unix >= since_unix]

    def snapshot_cells(self) -> list[Cell]:
        with self.lock:
            return list(self.cells.values())

    def snapshot_stats(self) -> dict[str, Any]:
        with self.lock:
            return dict(self.stats)

    def set_mqtt_connected(self, connected: bool) -> None:
        with self.lock:
            self.stats["mqtt_connected"] = connected
