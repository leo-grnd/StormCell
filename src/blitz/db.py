"""Persistance SQLite des impacts."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

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
"""


class Database:
    """Wrapper sqlite3 minimal, partagé entre threads grâce à check_same_thread=False."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) FROM strikes").fetchone()
        return int(row[0])

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
        try:
            self.conn.execute(
                "INSERT INTO strikes "
                "(ts_unix, ts_utc, lat, lon, distance_km, bearing_deg, mds, home_lat, home_lon) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ts_unix, ts_utc, lat, lon, distance_km, bearing_deg, mds, home_lat, home_lon),
            )
            self.conn.commit()
        except sqlite3.Error:
            logger.exception("Erreur d'insertion SQLite")

    def query_range(
        self,
        from_unix: float | None = None,
        to_unix: float | None = None,
        max_distance_km: float | None = None,
        min_mds: int | None = None,
        limit: int = 100_000,
    ) -> list[dict[str, Any]]:
        """Filtre par fenêtre temporelle / distance / nb détecteurs. Tri par ts_unix asc."""
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
        if min_mds is not None:
            conds.append("mds >= ?")
            params.append(min_mds)
        where = ("WHERE " + " AND ".join(conds)) if conds else ""
        sql = f"SELECT * FROM strikes {where} ORDER BY ts_unix ASC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()
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
        rows = self.conn.execute(sql, (f"-{int(days)} days",)).fetchall()
        return [dict(r) for r in rows]

    def date_bounds(self) -> tuple[float | None, float | None]:
        row = self.conn.execute("SELECT MIN(ts_unix), MAX(ts_unix) FROM strikes").fetchone()
        return (row[0], row[1])

    def close(self) -> None:
        try:
            self.conn.close()
        except sqlite3.Error:
            logger.exception("Erreur à la fermeture SQLite")
