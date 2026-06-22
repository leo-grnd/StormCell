"""Chargement de la configuration depuis un fichier TOML."""

from __future__ import annotations

import os
import re
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
class SourceConfig:
    # "blitzortung_ws" : flux WebSocket direct (≈ quelques s de latence, recommandé)
    # "mqtt"           : broker communautaire re-publié (latence ≈ 1-2 min)
    type: str = "blitzortung_ws"
    ws_endpoints: list[str] = field(default_factory=lambda: ["ws1", "ws7", "ws8"])
    ws_probe_seconds: float = 4.0   # durée de mesure de latence par endpoint au démarrage


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
class PredictConfig:
    min_probability: float = 0.3        # seuil de proba pour journaliser une prédiction
    verify_tolerance_min: float = 10.0  # fenêtre d'appariement prédiction ↔ arrivée
    arrival_gap_min: float = 20.0       # écart séparant deux événements d'arrivée


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
    source: SourceConfig = field(default_factory=SourceConfig)
    db: DbConfig = field(default_factory=DbConfig)
    web: WebConfig = field(default_factory=WebConfig)
    analysis: AnalysisConfig = field(default_factory=AnalysisConfig)
    predict: PredictConfig = field(default_factory=PredictConfig)
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

    if resolved is not None:
        with resolved.open("rb") as f:
            raw = tomllib.load(f)

        section_map = {
            "home": cfg.home,
            "filter": cfg.filter,
            "mqtt": cfg.mqtt,
            "source": cfg.source,
            "db": cfg.db,
            "web": cfg.web,
            "analysis": cfg.analysis,
            "predict": cfg.predict,
            "log": cfg.log,
        }
        for name, section in section_map.items():
            if name in raw and isinstance(raw[name], dict):
                _merge_section(section, raw[name])
        cfg.source_path = resolved

    # Override du chemin DB par variable d'environnement (pratique en conteneur).
    env_db = os.environ.get("BLITZ_DB")
    if env_db:
        cfg.db.path = env_db
    return cfg


def update_home(path: Path | str | None, lat: float, lon: float) -> bool:
    """Réécrit lat/lon de la section [home] d'un config.toml, en préservant le reste.

    Best-effort : renvoie False si le fichier est introuvable/illisible (la mise à
    jour en mémoire reste effective, seule la persistance échoue).
    """
    if path is None:
        return False
    p = Path(path)
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False

    def _set_value(line: str, value: float) -> str:
        # Remplace la valeur après '=' en gardant un éventuel commentaire de fin.
        return re.sub(r"(=\s*)[^#]*", rf"\g<1>{value}  ", line, count=1)

    out: list[str] = []
    in_home = False
    done_lat = done_lon = False
    has_home = any(ln.strip() == "[home]" for ln in lines)

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if in_home:  # on quitte [home] : compléter les clés manquantes
                if not done_lat:
                    out.append(f"lat = {lat}")
                    done_lat = True
                if not done_lon:
                    out.append(f"lon = {lon}")
                    done_lon = True
            in_home = stripped == "[home]"
            out.append(line)
            continue
        if in_home and re.match(r"\s*lat\s*=", line):
            out.append(_set_value(line, lat))
            done_lat = True
            continue
        if in_home and re.match(r"\s*lon\s*=", line):
            out.append(_set_value(line, lon))
            done_lon = True
            continue
        out.append(line)

    if in_home:
        if not done_lat:
            out.append(f"lat = {lat}")
        if not done_lon:
            out.append(f"lon = {lon}")
    if not has_home:
        out = ["[home]", f"lat = {lat}", f"lon = {lon}", ""] + out

    try:
        p.write_text("\n".join(out) + "\n", encoding="utf-8")
    except OSError:
        return False
    return True
