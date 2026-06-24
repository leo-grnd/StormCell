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
    retention_days: int = 0               # 0 = conservation illimitée ; sinon purge au-delà
    maintenance_interval_min: int = 60    # cadence de la maintenance (purge + checkpoint WAL)
    vacuum_on_maintenance: bool = False   # VACUUM après purge (récupère l'espace ; bloquant)


@dataclass
class WebConfig:
    host: str = "127.0.0.1"
    port: int = 8000


@dataclass
class AnalysisConfig:
    cluster_eps_km: float = 12.0
    cluster_min_samples: int = 4
    cluster_algo: str = "dbscan"          # "dbscan" | "hdbscan" (densité variable, multi-échelle)
    hdbscan_min_cluster_size: int = 8
    cell_window_minutes: int = 30
    tick_seconds: int = 3          # cadence du recalcul des cellules (robuste jusqu'à ~1 s)
    min_mds_quality: int = 0
    max_track_misses: int = 2
    eta_max_radius_km: float = 500.0
    max_strikes_for_clustering: int = 20_000
    # Pré-agrégation grille avant DBSCAN (Lot D #18) : sous un gros orage on regroupe
    # d'abord les strokes en cellules de grille fines (pondérées), puis on clusterise
    # ces représentants au lieu de re-clusteriser tout le buffer brut. 0 = désactivé.
    micro_grid_km: float = 1.5
    micro_cluster_min_points: int = 1500   # seuil d'activation de la pré-agrégation
    motion_history_seconds: float = 900.0
    recent_buffer: int = 50_000  # impacts gardés en mémoire vive (doit couvrir la fenêtre en gros orage)
    # ── Vague 2 : tracking Kalman + nowcast ──────────────────────────────────
    kf_meas_noise_km: float = 2.5        # bruit de mesure du centroïde (km)
    kf_process_accel: float = 2e-5       # bruit de process : accélération (km/s², écart-type)
    jump_window_min: float = 2.0         # fenêtre du taux d'éclairs instantané
    strike_ring_km: float = 15.0         # anneau « foudre arrivée » pour l'ETA au bord
    nowcast_horizon_min: float = 30.0    # horizon de la proba de coup sur HOME
    # ── P4 : a priori de mouvement régional pour les jeunes cellules ───────────
    regional_motion_prior: bool = True       # ETA provisoire emprunté au consensus régional
    regional_motion_min_cells: int = 2       # nb mini de cellules établies pour un consensus
    regional_motion_radius_km: float = 120.0  # une cellule établie doit être sous ce rayon
    # ── Vague 5 : regroupement strokes→flashs + lightning jump canonique (2σ) ──
    flash_dt_s: float = 0.5              # écart temporel max pour regrouper des strokes en un flash
    flash_dd_km: float = 10.0           # écart spatial max pour regrouper des strokes en un flash
    jump_min_flash_rate: float = 10.0   # plancher de taux (flashs/min) pour qu'un jump compte
    jump_dfrdt_lookback_s: float = 120.0  # fenêtre de la dérivée du taux (DFRDT, ≈ 2 min)
    jump_sigma_window_s: float = 720.0    # fenêtre d'estimation de σ du bruit (≈ 12 min)
    jump_sigma_mult: float = 2.0          # seuil en σ (canonique = 2)


@dataclass
class PredictConfig:
    min_probability: float = 0.3        # seuil de proba pour journaliser une prédiction
    verify_tolerance_min: float = 10.0  # fenêtre d'appariement prédiction ↔ arrivée
    arrival_gap_min: float = 20.0       # écart séparant deux événements d'arrivée


@dataclass
class OpsConfig:
    continuous_mode: bool = False     # « Mode 24/7 » activé (supervision continue)
    archive_path: str = ""            # base d'archive 24/7 (vide → <dossier db>/24-7/archive.db)


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
    ops: OpsConfig = field(default_factory=OpsConfig)
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
            "ops": cfg.ops,
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


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)   # round-trip complet (ne tronque pas les coordonnées)
    return f'"{v}"'


def update_config(path: Path | str | None, section_values: dict[str, dict[str, object]]) -> bool:
    """Met à jour des clés de plusieurs sections d'un config.toml, en préservant le reste.

    `section_values` ex. : {"home": {"lat": 44.2, "lon": 4.7}, "filter": {"max_distance_km": 150}}.
    Best-effort : renvoie False si le fichier est introuvable/illisible (la mise à jour
    en mémoire reste effective, seule la persistance échoue).
    """
    if path is None:
        return False
    p = Path(path)
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False

    remaining = {sec: dict(kv) for sec, kv in section_values.items()}
    out: list[str] = []
    cur: str | None = None

    def flush(section: str | None) -> None:
        if section in remaining and remaining[section]:
            for k, v in remaining[section].items():
                out.append(f"{k} = {_toml_value(v)}")
            remaining[section] = {}

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            flush(cur)                       # compléter les clés manquantes de la section quittée
            cur = stripped[1:-1]
            out.append(line)
            continue
        if cur in remaining and remaining[cur]:
            m = re.match(r"(\s*)([A-Za-z0-9_]+)(\s*=\s*)", line)
            if m and m.group(2) in remaining[cur]:
                key = m.group(2)
                value = remaining[cur].pop(key)
                hash_idx = line.find("#")
                comment = ("  " + line[hash_idx:]) if hash_idx != -1 else ""
                out.append(f"{m.group(1)}{key} = {_toml_value(value)}{comment}")
                continue
        out.append(line)

    flush(cur)                               # dernière section du fichier
    for sec, kv in remaining.items():        # sections totalement absentes
        if kv:
            out.append(f"[{sec}]")
            for k, v in kv.items():
                out.append(f"{k} = {_toml_value(v)}")

    try:
        p.write_text("\n".join(out) + "\n", encoding="utf-8")
    except OSError:
        return False
    return True


def update_home(path: Path | str | None, lat: float, lon: float) -> bool:
    """Réécrit lat/lon de la section [home] (compat — délègue à update_config)."""
    return update_config(path, {"home": {"lat": lat, "lon": lon}})
