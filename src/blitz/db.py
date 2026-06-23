"""Persistance SQLite des impacts.

Thread-safety : une seule connexion est partagée entre le thread réseau MQTT
(écritures), les threads du pool FastAPI (lectures) et un thread flusher interne.
Tous les accès à la connexion passent par `self._lock`.

Écritures batchées : `insert_strike` met en tampon et un `executemany` est émis
soit quand le tampon atteint `flush_threshold`, soit toutes les `flush_interval`
secondes (thread flusher), au lieu d'un `commit` par impact dans le thread MQTT.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS strikes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_unix     REAL    NOT NULL,
    ts_utc      TEXT    NOT NULL,
    lat         REAL    NOT NULL,
    lon         REAL    NOT NULL,
    distance_km REAL    NOT NULL,
    bearing_deg REAL    NOT NULL,
    mds         INTEGER,
    home_lat    REAL    NOT NULL,
    home_lon    REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_strikes_ts   ON strikes(ts_unix);
CREATE INDEX IF NOT EXISTS idx_strikes_dist ON strikes(distance_km);
-- Index composite : sert les requêtes « fenêtre temporelle ∧ anneau » (history, anneau d'alerte).
CREATE INDEX IF NOT EXISTS idx_strikes_ts_dist ON strikes(ts_unix, distance_km);

-- Catalogue des cellules orageuses détectées (une ligne par cellule et par run).
CREATE TABLE IF NOT EXISTS cells (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT    NOT NULL,   -- identifiant du process (les cell_id repartent à 1 par run)
    cell_id         INTEGER NOT NULL,
    first_seen      REAL    NOT NULL,
    last_seen       REAL    NOT NULL,
    peak_flash_rate REAL,
    max_radius_km   REAL,
    max_severity    REAL,
    total_strikes   INTEGER,
    parent_id       INTEGER,
    UNIQUE(run_id, cell_id)
);
CREATE INDEX IF NOT EXISTS idx_cells_lastseen ON cells(last_seen);

-- Points de trajectoire (un par tick où la cellule est vivante).
CREATE TABLE IF NOT EXISTS cell_track (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id    TEXT    NOT NULL,
    cell_id   INTEGER NOT NULL,
    ts_unix   REAL    NOT NULL,
    lat       REAL    NOT NULL,
    lon       REAL    NOT NULL,
    radius_km REAL,
    strikes_count       INTEGER,
    flash_rate_per_min  REAL,
    velocity_kmh        REAL,
    heading_deg         REAL,
    severity            REAL,
    eta_minutes         REAL,
    eta_strike_minutes  REAL,
    closest_approach_km REAL,
    strike_probability  REAL,
    jump_detected       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_celltrack ON cell_track(run_id, cell_id, ts_unix);

-- Journal des prédictions émises (pour la vérification / skill score).
CREATE TABLE IF NOT EXISTS predictions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id            TEXT    NOT NULL,
    cell_id           INTEGER NOT NULL,
    ts_made           REAL    NOT NULL,   -- instant où la prédiction a été émise
    eta_strike_min    REAL,
    predicted_arrival REAL,               -- ts_made + eta_strike_min*60
    probability       REAL,
    closest_km        REAL
);
CREATE INDEX IF NOT EXISTS idx_predictions_ts ON predictions(ts_made);

-- Cache de géocodage inverse (commune la plus proche d'un point).
CREATE TABLE IF NOT EXISTS geocache (
    key   TEXT PRIMARY KEY,   -- "lat,lon" arrondi (~1 km)
    name  TEXT,
    ts    REAL
);
"""

_INSERT_SQL = (
    "INSERT INTO strikes "
    "(ts_unix, ts_utc, lat, lon, distance_km, bearing_deg, mds, home_lat, home_lon) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


class Database:
    """Wrapper sqlite3, sérialisé par un verrou, avec écritures batchées."""

    def __init__(
        self,
        path: Path,
        *,
        flush_interval: float = 2.0,
        flush_threshold: int = 200,
        retention_days: int = 0,
        maintenance_interval_min: int = 60,
        vacuum_on_maintenance: bool = False,
    ) -> None:
        self.path = path
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        # Tuning lecture/écriture : cache page de 64 MiB, mmap 256 MiB, temp en RAM.
        self.conn.execute("PRAGMA cache_size=-65536")      # négatif ⇒ KiB (≈ 64 MiB)
        self.conn.execute("PRAGMA mmap_size=268435456")    # 256 MiB
        self.conn.execute("PRAGMA temp_store=MEMORY")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

        self._write_buf: list[tuple] = []
        self._flush_threshold = flush_threshold
        self._flush_interval = flush_interval
        self.retention_days = retention_days
        self._maint_interval_s = max(60.0, maintenance_interval_min * 60.0)
        self.vacuum_on_maintenance = vacuum_on_maintenance
        self._closed = False
        self._flusher = threading.Thread(target=self._flush_loop, name="db-flusher", daemon=True)
        self._flusher.start()
        self._maint = threading.Thread(target=self._maint_loop, name="db-maint", daemon=True)
        self._maint.start()

    # ── écritures (batch) ────────────────────────────────────────────────────
    def insert_strike(
        self,
        ts_unix: float,
        lat: float,
        lon: float,
        distance_km: float,
        bearing_deg: float,
        mds: int | None,
        home_lat: float,
        home_lon: float,
    ) -> None:
        ts_utc = datetime.fromtimestamp(ts_unix, tz=timezone.utc).isoformat()
        row = (ts_unix, ts_utc, lat, lon, distance_km, bearing_deg, mds, home_lat, home_lon)
        with self._lock:
            self._write_buf.append(row)
            if len(self._write_buf) >= self._flush_threshold:
                self._flush_locked()

    def _flush_locked(self) -> None:
        """Écrit le tampon. À appeler avec `self._lock` déjà acquis."""
        if not self._write_buf:
            return
        try:
            self.conn.executemany(_INSERT_SQL, self._write_buf)
            self.conn.commit()
            self._write_buf.clear()
        except sqlite3.Error:
            logger.exception("Erreur de flush SQLite (%d lignes en attente)", len(self._write_buf))

    def flush(self) -> None:
        with self._lock:
            self._flush_locked()

    def _flush_loop(self) -> None:
        while not self._closed:
            time.sleep(self._flush_interval)
            try:
                self.flush()
            except Exception:
                logger.exception("Erreur dans le thread flusher SQLite")

    # ── maintenance (rétention / checkpoint / vacuum) ────────────────────────
    def purge_older_than(self, cutoff_unix: float) -> int:
        """Supprime impacts, trajectoires, prédictions et cellules plus vieux que `cutoff`.
        Renvoie le nombre d'impacts supprimés."""
        with self._lock:
            self._flush_locked()
            try:
                deleted = self.conn.execute(
                    "DELETE FROM strikes WHERE ts_unix < ?", (cutoff_unix,)
                ).rowcount
                self.conn.execute("DELETE FROM cell_track WHERE ts_unix < ?", (cutoff_unix,))
                self.conn.execute("DELETE FROM predictions WHERE ts_made < ?", (cutoff_unix,))
                self.conn.execute("DELETE FROM cells WHERE last_seen < ?", (cutoff_unix,))
                self.conn.commit()
                return int(deleted or 0)
            except sqlite3.Error:
                logger.exception("Erreur de purge SQLite")
                return 0

    def maintain(self) -> dict[str, Any]:
        """Purge (si rétention), checkpoint du WAL, et VACUUM optionnel."""
        result: dict[str, Any] = {"deleted": 0, "checkpointed": False, "vacuumed": False}
        if self.retention_days and self.retention_days > 0:
            cutoff = time.time() - self.retention_days * 86400.0
            result["deleted"] = self.purge_older_than(cutoff)
        with self._lock:
            try:
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                # Laisse SQLite rafraîchir ses stats d'index (requêtes history plus rapides).
                self.conn.execute("PRAGMA optimize")
                result["checkpointed"] = True
            except sqlite3.Error:
                logger.exception("Erreur checkpoint WAL")
        # VACUUM uniquement si on a réellement libéré des lignes (sinon coûteux pour rien).
        if self.vacuum_on_maintenance and result["deleted"] > 0:
            with self._lock:
                try:
                    self.conn.execute("VACUUM")
                    result["vacuumed"] = True
                except sqlite3.Error:
                    logger.exception("Erreur VACUUM")
        return result

    def _maint_loop(self) -> None:
        while not self._closed:
            # Sommeil fractionné pour réagir vite à la fermeture.
            slept = 0.0
            while slept < self._maint_interval_s and not self._closed:
                time.sleep(min(5.0, self._maint_interval_s - slept))
                slept += 5.0
            if self._closed:
                break
            try:
                res = self.maintain()
                if res["deleted"]:
                    logger.info(
                        "Maintenance DB : %d impacts purgés (rétention %d j)%s",
                        res["deleted"], self.retention_days,
                        " + VACUUM" if res["vacuumed"] else "",
                    )
            except Exception:
                logger.exception("Erreur dans le thread de maintenance SQLite")

    # ── lectures (flush d'abord pour voir les écritures récentes) ────────────
    def count(self) -> int:
        with self._lock:
            self._flush_locked()
            row = self.conn.execute("SELECT COUNT(*) FROM strikes").fetchone()
        return int(row[0])

    def query_range(
        self,
        from_unix: float | None = None,
        to_unix: float | None = None,
        max_distance_km: float | None = None,
        min_mds: int | None = None,
        limit: int = 100_000,
    ) -> list[dict[str, Any]]:
        """Filtre par fenêtre temporelle / distance / nb détecteurs. Tri par ts_unix asc."""
        where, params = self._range_where(from_unix, to_unix, max_distance_km, min_mds)
        sql = f"SELECT * FROM strikes {where} ORDER BY ts_unix ASC LIMIT ?"
        params.append(limit)
        with self._lock:
            self._flush_locked()
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def _range_where(
        from_unix: float | None, to_unix: float | None,
        max_distance_km: float | None, min_mds: int | None,
    ) -> tuple[str, list[Any]]:
        conds: list[str] = []
        params: list[Any] = []
        if from_unix is not None:
            conds.append("ts_unix >= ?")
            params.append(from_unix)
        if to_unix is not None:
            conds.append("ts_unix <= ?")
            params.append(to_unix)
        if max_distance_km is not None:
            conds.append("distance_km <= ?")
            params.append(max_distance_km)
        if min_mds:
            conds.append("mds >= ?")
            params.append(min_mds)
        where = ("WHERE " + " AND ".join(conds)) if conds else ""
        return where, params

    def count_range(
        self, from_unix: float | None = None, to_unix: float | None = None,
        max_distance_km: float | None = None, min_mds: int | None = None,
    ) -> int:
        """Nombre d'impacts qui correspondent au filtre (sert à décider la décimation)."""
        where, params = self._range_where(from_unix, to_unix, max_distance_km, min_mds)
        with self._lock:
            self._flush_locked()
            row = self.conn.execute(f"SELECT COUNT(*) FROM strikes {where}", params).fetchone()
        return int(row[0])

    def query_range_decimated(
        self, grid_deg: float,
        from_unix: float | None = None, to_unix: float | None = None,
        max_distance_km: float | None = None, min_mds: int | None = None,
        limit: int = 12_000,
    ) -> list[dict[str, Any]]:
        """Agrège les impacts sur une grille lat/lon de pas `grid_deg`.

        Renvoie un point par cellule de grille occupée : centre arrondi, nombre
        d'impacts `n`, distance la plus proche et timestamp le plus récent. Permet
        d'afficher une heatmap d'une longue période sans transférer 500k points.
        """
        g = max(grid_deg, 1e-4)
        where, params = self._range_where(from_unix, to_unix, max_distance_km, min_mds)
        sql = (
            f"SELECT ROUND(lat/?)*? AS lat, ROUND(lon/?)*? AS lon, "
            f"COUNT(*) AS n, MIN(distance_km) AS distance_km, MAX(ts_unix) AS ts_unix "
            f"FROM strikes {where} GROUP BY 1, 2 ORDER BY n DESC LIMIT ?"
        )
        full_params = [g, g, g, g, *params, limit]
        with self._lock:
            self._flush_locked()
            rows = self.conn.execute(sql, full_params).fetchall()
        return [dict(r) for r in rows]

    def count_per_hour(self, days: int = 30) -> list[dict[str, Any]]:
        """Nombre d'impacts par heure sur les `days` derniers jours (UTC)."""
        sql = """
            SELECT strftime('%Y-%m-%d %H:00', ts_utc) AS hour, COUNT(*) AS n
            FROM strikes
            WHERE ts_unix >= strftime('%s', 'now', ?) * 1.0
            GROUP BY hour
            ORDER BY hour ASC
        """
        with self._lock:
            self._flush_locked()
            rows = self.conn.execute(sql, (f"-{int(days)} days",)).fetchall()
        return [dict(r) for r in rows]

    def date_bounds(self) -> tuple[float | None, float | None]:
        with self._lock:
            self._flush_locked()
            row = self.conn.execute("SELECT MIN(ts_unix), MAX(ts_unix) FROM strikes").fetchone()
        return (row[0], row[1])

    # ── persistance des cellules / trajectoires ──────────────────────────────
    def upsert_cells(self, run_id: str, rows: list[dict[str, Any]]) -> None:
        """Insère/agrège les cellules (clé run_id+cell_id). Garde les pics."""
        if not rows:
            return
        sql = """
            INSERT INTO cells
                (run_id, cell_id, first_seen, last_seen, peak_flash_rate,
                 max_radius_km, max_severity, total_strikes, parent_id)
            VALUES (:run_id, :cell_id, :first_seen, :last_seen, :flash,
                    :radius, :sev, :strikes, :parent)
            ON CONFLICT(run_id, cell_id) DO UPDATE SET
                last_seen       = excluded.last_seen,
                peak_flash_rate = MAX(COALESCE(cells.peak_flash_rate, 0), COALESCE(excluded.peak_flash_rate, 0)),
                max_radius_km   = MAX(COALESCE(cells.max_radius_km, 0),   COALESCE(excluded.max_radius_km, 0)),
                max_severity    = MAX(COALESCE(cells.max_severity, 0),    COALESCE(excluded.max_severity, 0)),
                total_strikes   = MAX(COALESCE(cells.total_strikes, 0),   COALESCE(excluded.total_strikes, 0)),
                parent_id       = COALESCE(cells.parent_id, excluded.parent_id)
        """
        params = [
            {
                "run_id": run_id, "cell_id": c["cell_id"],
                "first_seen": c["first_seen"], "last_seen": c["last_seen"],
                "flash": c.get("flash_rate_per_min"), "radius": c["radius_km"],
                "sev": c.get("severity"), "strikes": c["strikes_count"],
                "parent": c.get("parent_id"),
            }
            for c in rows
        ]
        with self._lock:
            try:
                self.conn.executemany(sql, params)
                self.conn.commit()
            except sqlite3.Error:
                logger.exception("Erreur upsert cells")

    def insert_cell_tracks(self, run_id: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        sql = """
            INSERT INTO cell_track
                (run_id, cell_id, ts_unix, lat, lon, radius_km, strikes_count,
                 flash_rate_per_min, velocity_kmh, heading_deg, severity,
                 eta_minutes, eta_strike_minutes, closest_approach_km,
                 strike_probability, jump_detected)
            VALUES (:run_id, :cell_id, :ts, :lat, :lon, :radius, :strikes,
                    :flash, :vel, :hdg, :sev, :eta, :etas, :closest, :prob, :jump)
        """
        params = [
            {
                "run_id": run_id, "cell_id": c["cell_id"], "ts": c["last_seen"],
                "lat": c["centroid"]["lat"], "lon": c["centroid"]["lon"],
                "radius": c["radius_km"], "strikes": c["strikes_count"],
                "flash": c.get("flash_rate_per_min"), "vel": c.get("velocity_kmh"),
                "hdg": c.get("heading_deg"), "sev": c.get("severity"),
                "eta": c.get("eta_minutes"), "etas": c.get("eta_strike_minutes"),
                "closest": c.get("closest_approach_km"), "prob": c.get("strike_probability"),
                "jump": 1 if c.get("jump_detected") else 0,
            }
            for c in rows
        ]
        with self._lock:
            try:
                self.conn.executemany(sql, params)
                self.conn.commit()
            except sqlite3.Error:
                logger.exception("Erreur insert cell_track")

    def list_cells(
        self, from_unix: float | None = None, to_unix: float | None = None,
        min_strikes: int = 0, limit: int = 500,
    ) -> list[dict[str, Any]]:
        conds, params = [], []
        if from_unix is not None:
            conds.append("last_seen >= ?")
            params.append(from_unix)
        if to_unix is not None:
            conds.append("first_seen <= ?")
            params.append(to_unix)
        if min_strikes:
            conds.append("total_strikes >= ?")
            params.append(min_strikes)
        where = ("WHERE " + " AND ".join(conds)) if conds else ""
        sql = f"SELECT * FROM cells {where} ORDER BY last_seen DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def cell_track(self, run_id: str, cell_id: int) -> list[dict[str, Any]]:
        sql = "SELECT * FROM cell_track WHERE run_id = ? AND cell_id = ? ORDER BY ts_unix ASC"
        with self._lock:
            rows = self.conn.execute(sql, (run_id, cell_id)).fetchall()
        return [dict(r) for r in rows]

    # ── prédictions / vérification ───────────────────────────────────────────
    def log_predictions(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        sql = """
            INSERT INTO predictions
                (run_id, cell_id, ts_made, eta_strike_min, predicted_arrival, probability, closest_km)
            VALUES (:run_id, :cell_id, :ts_made, :eta, :pa, :prob, :closest)
        """
        with self._lock:
            try:
                self.conn.executemany(sql, rows)
                self.conn.commit()
            except sqlite3.Error:
                logger.exception("Erreur log predictions")

    def predictions_between(self, from_unix: float, to_unix: float) -> list[dict[str, Any]]:
        sql = "SELECT * FROM predictions WHERE ts_made >= ? AND ts_made <= ? ORDER BY ts_made ASC"
        with self._lock:
            rows = self.conn.execute(sql, (from_unix, to_unix)).fetchall()
        return [dict(r) for r in rows]

    def strike_ts_in_ring(self, ring_km: float, from_unix: float, to_unix: float) -> list[float]:
        """Timestamps des impacts entrés dans l'anneau (distance ≤ ring) sur la fenêtre."""
        sql = (
            "SELECT ts_unix FROM strikes "
            "WHERE distance_km <= ? AND ts_unix >= ? AND ts_unix <= ? ORDER BY ts_unix ASC"
        )
        with self._lock:
            self._flush_locked()
            rows = self.conn.execute(sql, (ring_km, from_unix, to_unix)).fetchall()
        return [float(r[0]) for r in rows]

    # ── analytics historiques ────────────────────────────────────────────────
    def _grouped(self, sql: str, days: int, extra: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            self._flush_locked()
            rows = self.conn.execute(sql, (f"-{int(days)} days", *extra)).fetchall()
        return [dict(r) for r in rows]

    def count_by_hour_of_day(self, days: int = 365) -> list[dict[str, Any]]:
        return self._grouped(
            "SELECT CAST(strftime('%H', ts_utc) AS INT) AS hour, COUNT(*) AS n "
            "FROM strikes WHERE ts_unix >= strftime('%s','now',?)*1.0 GROUP BY hour ORDER BY hour",
            days,
        )

    def count_by_weekday(self, days: int = 365) -> list[dict[str, Any]]:
        return self._grouped(
            "SELECT CAST(strftime('%w', ts_utc) AS INT) AS weekday, COUNT(*) AS n "
            "FROM strikes WHERE ts_unix >= strftime('%s','now',?)*1.0 GROUP BY weekday ORDER BY weekday",
            days,
        )

    def bearing_rose(self, days: int = 365) -> list[dict[str, Any]]:
        return self._grouped(
            "SELECT CAST(((bearing_deg + 11.25) / 22.5) AS INT) % 16 AS sector, COUNT(*) AS n "
            "FROM strikes WHERE ts_unix >= strftime('%s','now',?)*1.0 GROUP BY sector ORDER BY sector",
            days,
        )

    def distance_histogram(self, days: int = 365, bin_km: float = 10.0, max_km: float = 300.0) -> list[dict[str, Any]]:
        sql = (
            "SELECT CAST(distance_km / ? AS INT) AS bin, COUNT(*) AS n "
            "FROM strikes WHERE distance_km <= ? AND ts_unix >= strftime('%s','now',?)*1.0 "
            "GROUP BY bin ORDER BY bin"
        )
        with self._lock:
            self._flush_locked()
            rows = self.conn.execute(sql, (bin_km, max_km, f"-{int(days)} days")).fetchall()
        return [dict(r) for r in rows]

    # ── cache de géocodage ───────────────────────────────────────────────────
    def geocode_get(self, key: str) -> str | None:
        with self._lock:
            row = self.conn.execute("SELECT name FROM geocache WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def geocode_put(self, key: str, name: str) -> None:
        with self._lock:
            try:
                self.conn.execute(
                    "INSERT OR REPLACE INTO geocache (key, name, ts) VALUES (?, ?, ?)",
                    (key, name, time.time()),
                )
                self.conn.commit()
            except sqlite3.Error:
                logger.exception("Erreur écriture geocache")

    def close(self) -> None:
        self._closed = True
        try:
            with self._lock:
                self._flush_locked()
                try:
                    self.conn.execute("PRAGMA optimize")   # recommandé avant fermeture
                except sqlite3.Error:
                    pass
                self.conn.close()
        except sqlite3.Error:
            logger.exception("Erreur à la fermeture SQLite")
