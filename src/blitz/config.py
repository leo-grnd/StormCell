"""Chargement de la configuration depuis un fichier TOML."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class HomeConfig:
    lat: float = 44.243318
    lon: float = 4.716102


@dataclass
class FilterConfig:
    max_distance_km: float = 100.0
    max_strikes_shown: int = 15
    alert_distance_km: float = 15.0


@dataclass
class MqttConfig:
    host: str = "blitzortung.ha.sed.pl"
    port: int = 1883
    topic: str = "blitzortung/1.1/#"
    reconnect_min_s: int = 1
    reconnect_max_s: int = 60
    keepalive_s: int = 60


@dataclass
class DbConfig:
    path: str = "lightning_log.db"


@dataclass
class WebConfig:
    host: str = "127.0.0.1"
    port: int = 8000


@dataclass
class AnalysisConfig:
    cluster_eps_km: float = 12.0
    cluster_min_samples: int = 4
    cell_window_minutes: int = 30
    tick_seconds: int = 7
    min_mds_quality: int = 0
    max_track_misses: int = 2
    eta_max_radius_km: float = 500.0
    max_strikes_for_clustering: int = 20_000
    motion_history_seconds: float = 900.0
    recent_buffer: int = 50_000  # impacts gardés en mémoire vive (doit couvrir la fenêtre en gros orage)
    # ── Vague 2 : tracking Kalman + nowcast ──────────────────────────────────
    kf_meas_noise_km: float = 2.5        # bruit de mesure du centroïde (km)
    kf_process_accel: float = 2e-5       # bruit de process : accélération (km/s², écart-type)
    jump_window_min: float = 2.0         # fenêtre du taux d'éclairs instantané
    strike_ring_km: float = 15.0         # anneau « foudre arrivée » pour l'ETA au bord
    nowcast_horizon_min: float = 30.0    # horizon de la proba de coup sur HOME


@dataclass
class LogConfig:
    file: str = "blitz.log"
    max_bytes: int = 10 * 1024 * 1024
    backups: int = 3


@dataclass
class Config:
    home: HomeConfig = field(default_factory=HomeConfig)
    filter: FilterConfig = field(default_factory=FilterConfig)
    mqtt: MqttConfig = field(default_factory=MqttConfig)
    db: DbConfig = field(default_factory=DbConfig)
    web: WebConfig = field(default_factory=WebConfig)
    analysis: AnalysisConfig = field(default_factory=AnalysisConfig)
    log: LogConfig = field(default_factory=LogConfig)
    source_path: Path | None = None


def _merge_section(section: object, raw: dict) -> None:
    for key, value in raw.items():
        if hasattr(section, key):
            setattr(section, key, value)


def load_config(path: Path | str | None = None) -> Config:
    """Charge la config.

    Ordre de résolution : `path` explicite > `./config.toml` > défauts hardcodés.
    Les clés absentes du fichier conservent leur valeur par défaut.
    """
    cfg = Config()
    resolved: Path | None = None
    if path is not None:
        candidate = Path(path)
        if candidate.exists():
            resolved = candidate
    else:
        candidate = Path("config.toml")
        if candidate.exists():
            resolved = candidate

    if resolved is None:
        return cfg

    with resolved.open("rb") as f:
        raw = tomllib.load(f)

    section_map = {
        "home": cfg.home,
        "filter": cfg.filter,
        "mqtt": cfg.mqtt,
        "db": cfg.db,
        "web": cfg.web,
        "analysis": cfg.analysis,
        "log": cfg.log,
    }
    for name, section in section_map.items():
        if name in raw and isinstance(raw[name], dict):
            _merge_section(section, raw[name])

    cfg.source_path = resolved
    return cfg
