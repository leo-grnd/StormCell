"""Serveur FastAPI : REST + WebSocket + worker MQTT en arrière-plan."""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import queue
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .analysis import filter_window, update_cells
from .blitz_ws import BlitzortungWsWorker, probe_endpoints
from .config import Config, update_config, update_home
from .db import Database
from .geo import haversine
from .geocode import reverse_nominatim
from .mqtt_worker import MqttWorker
from .state import SharedState, Strike
from .verification import evaluate


class HomeIn(BaseModel):
    lat: float
    lon: float


class ConfigIn(BaseModel):
    home_lat: float | None = None
    home_lon: float | None = None
    max_distance_km: float | None = None
    alert_distance_km: float | None = None
    cluster_eps_km: float | None = None
    cluster_min_samples: int | None = None
    cell_window_minutes: int | None = None
    min_mds_quality: int | None = None
    tick_seconds: int | None = None
    strike_ring_km: float | None = None


class RetentionIn(BaseModel):
    days: int


class ModeIn(BaseModel):
    enabled: bool

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent / "web"

# ── Historique : décimation serveur (Lot D #19) ──────────────────────────────
HIST_DECIMATE_THRESHOLD = 25_000   # au-delà, on agrège sur grille
HIST_TARGET_POINTS = 8_000         # nb de points visé après agrégation


def _hist_grid_deg(total: int) -> float:
    """Pas de grille (degrés) pour ramener `total` impacts à ≈ HIST_TARGET_POINTS.

    Heuristique : la densité de cellules occupées croît ~ comme la racine du nombre
    de points, donc on dimensionne le pas en √(total/cible). Borné à [0.01°, 0.5°]
    (≈ 1 km à 55 km) pour rester lisible.
    """
    scale = max(1.0, (total / HIST_TARGET_POINTS) ** 0.5)
    return min(0.5, max(0.01, round(0.01 * scale, 4)))


class Hub:
    """Diffuse les nouveaux impacts et les recalculs de cellules vers les WS connectées."""

    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()
        self.lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self.lock:
            self.clients.add(ws)

    async def disconnect(self, ws: WebSocket) -> None:
        async with self.lock:
            self.clients.discard(ws)

    async def broadcast(self, message: dict) -> None:
        async with self.lock:
            stale: list[WebSocket] = []
            for ws in self.clients:
                try:
                    await ws.send_json(message)
                except Exception:
                    stale.append(ws)
            for ws in stale:
                self.clients.discard(ws)


class AppContext:
    """Singleton contenant la config et les composants live (créé au startup)."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.state = SharedState(max_strikes_recent=config.analysis.recent_buffer)
        self.db = Database(
            Path(config.db.path),
            retention_days=config.db.retention_days,
            maintenance_interval_min=config.db.maintenance_interval_min,
            vacuum_on_maintenance=config.db.vacuum_on_maintenance,
        )
        self.state.stats["logged_total"] = self.db.count()
        self.hub = Hub()
        self.worker: MqttWorker | BlitzortungWsWorker | None = None
        self._next_cell_id = 1
        # ── Vague 3 : mémoire & vérification ─────────────────────────────────
        self.run_id = uuid.uuid4().hex[:12]   # les cell_id repartent à 1 à chaque run
        self._persisted_track_ts: dict[int, float] = {}
        self._open_warnings: set[int] = set()
        # ── Mode 24/7 : base d'archive dédiée (séparée de la base normale) ────
        self.archive: Database | None = None
        self.archive_started_at: float | None = None

    def archive_path(self) -> Path:
        """Chemin de la base d'archive 24/7 (configurable ; défaut <dossier db>/24-7/archive.db)."""
        custom = (self.config.ops.archive_path or "").strip()
        if custom:
            return Path(custom)
        return Path(self.config.db.path).resolve().parent / "24-7" / "archive.db"

    def set_continuous(self, enabled: bool) -> None:
        """Active/désactive le mode 24/7 : ouvre (ou ferme) la base d'archive dédiée.

        L'archive n'a JAMAIS de rétention → elle n'est pas affectée par les purges de
        la base normale. La capture y est dupliquée tant que le mode est actif.
        """
        self.config.ops.continuous_mode = bool(enabled)
        if enabled and self.archive is None:
            path = self.archive_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            self.archive = Database(path)   # retention_days=0 → jamais purgée
            self.archive_started_at = time.time()
            logger.info("Mode 24/7 ON — archive ouverte : %s", path)
        elif not enabled and self.archive is not None:
            try:
                self.archive.close()
            except Exception:
                logger.exception("Erreur à la fermeture de l'archive 24/7")
            self.archive = None
            self.archive_started_at = None
            logger.info("Mode 24/7 OFF — archive fermée")

    def live_stats(self) -> dict:
        """Snapshot des stats enrichi des métriques de débit de la queue de diffusion."""
        snap = self.state.snapshot_stats()
        q = getattr(self.worker, "queue", None)
        if q is not None:
            snap["queue_depth"] = q.qsize()
            snap["queue_max"] = q.maxsize
        return snap

    def on_nearby(self, s: Strike) -> None:
        """Listener appelé par le worker pour chaque impact dans la zone.
        Écrit en base normale et, si le mode 24/7 est actif, aussi dans l'archive."""
        for db in (self.db, self.archive):
            if db is None:
                continue
            db.insert_strike(
                ts_unix=s.ts_unix,
                lat=s.lat,
                lon=s.lon,
                distance_km=s.distance_km,
                bearing_deg=s.bearing_deg,
                mds=s.mds,
                home_lat=self.config.home.lat,
                home_lon=self.config.home.lon,
            )
        with self.state.lock:
            self.state.stats["logged_session"] += 1
            self.state.stats["logged_total"] += 1


def _iso_to_unix(s: str | None) -> float | None:
    if s is None:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Date invalide : {s}") from None


def create_app(config: Config) -> FastAPI:
    """Construit l'app FastAPI. Le worker MQTT et la boucle d'analyse démarrent au startup."""
    ctx = AppContext(config)

    async def mqtt_pump() -> None:
        """Récupère les impacts depuis la queue paho (thread) et notifie les WS via asyncio."""
        loop = asyncio.get_running_loop()
        while True:
            try:
                strike: Strike = await loop.run_in_executor(None, ctx.worker.queue.get, True, 1.0)  # type: ignore[union-attr]
            except queue.Empty:
                await asyncio.sleep(0)
                continue
            except Exception:
                await asyncio.sleep(0.5)
                continue
            await ctx.hub.broadcast({"type": "strike", "data": strike.to_dict()})

    async def cells_loop() -> None:
        """Recalcule les cellules à intervalle régulier et notifie les WS.

        DBSCAN + tracking sont CPU-bound : on les exécute dans un thread pool
        pour ne jamais bloquer l'event-loop (et donc les WebSockets).
        """
        loop = asyncio.get_running_loop()
        while True:
            # Cadence stable et robuste aux tics rapides (3 s) : on chronomètre le cycle
            # et on dort le *reste* du tick. Comme la boucle est séquentielle (await), un
            # recalcul lent ne s'empile jamais ; on garde au moins 0.2 s de répit pour
            # toujours rendre la main même si un tick dépasse l'intervalle.
            cycle_start = loop.time()
            try:
                with ctx.state.lock:
                    recent = list(ctx.state.recent)
                    previous = dict(ctx.state.cells)

                def _recompute(recent=recent, previous=previous) -> tuple[dict, int]:
                    max_d = config.filter.max_distance_km
                    windowed = [
                        s for s in filter_window(recent, config.analysis.cell_window_minutes)
                        if s.distance_km <= max_d
                    ]
                    return update_cells(
                        config.home, windowed, previous, config.analysis,
                        next_id=ctx._next_cell_id,
                    )

                t0 = time.perf_counter()
                cells, next_id = await loop.run_in_executor(None, _recompute)
                ctx.state.set_cells_metrics((time.perf_counter() - t0) * 1000.0, len(cells))
                ctx._next_cell_id = next_id
                with ctx.state.lock:
                    ctx.state.cells = cells
                await ctx.hub.broadcast(
                    {"type": "cells", "data": [c.to_dict() for c in cells.values()]}
                )
                # Persistance (catalogue + trajectoires) et journal des prédictions.
                await run_in_threadpool(_persist_and_predict, cells)
            except Exception:
                logger.exception("Erreur dans la boucle d'analyse")
            elapsed = loop.time() - cycle_start
            await asyncio.sleep(max(0.2, config.analysis.tick_seconds - elapsed))

    def _persist_and_predict(cells: dict) -> None:
        """Écrit les cellules/trajectoires en DB et journalise les nouvelles alertes."""
        now2 = time.time()
        min_prob = config.predict.min_probability
        live_dicts: list[dict] = []
        track_rows: list[dict] = []
        new_preds: list[dict] = []
        current_warn: set[int] = set()

        for cell in cells.values():
            cd = cell.to_dict()
            cid = cd["cell_id"]
            if cell.misses == 0:
                live_dicts.append(cd)
                if cd["last_seen"] > ctx._persisted_track_ts.get(cid, 0.0):
                    track_rows.append(cd)
                    ctx._persisted_track_ts[cid] = cd["last_seen"]
            prob = cd.get("strike_probability") or 0.0
            eta_s = cd.get("eta_strike_minutes")
            # On ne journalise pas les ETA empruntés (P4) : la vérification ne doit
            # contenir que des prédictions issues d'un vrai suivi de la cellule.
            if eta_s is not None and prob >= min_prob and not cd.get("motion_provisional"):
                current_warn.add(cid)
                if cid not in ctx._open_warnings:   # nouvelle alerte → on la journalise une fois
                    new_preds.append({
                        "run_id": ctx.run_id, "cell_id": cid, "ts_made": now2,
                        "eta": eta_s, "pa": now2 + eta_s * 60.0,
                        "prob": prob, "closest": cd.get("closest_approach_km"),
                    })

        for db in (ctx.db, ctx.archive):
            if db is None:
                continue
            db.upsert_cells(ctx.run_id, live_dicts)
            db.insert_cell_tracks(ctx.run_id, track_rows)
            db.log_predictions(new_preds)
        ctx._open_warnings = current_warn
        # Purge du registre de timestamps (évite la croissance sur un run très long en 24/7).
        ctx._persisted_track_ts = {
            cid: ts for cid, ts in ctx._persisted_track_ts.items() if cid in cells
        }

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if config.source.type == "mqtt":
            ctx.worker = MqttWorker(config, ctx.state, on_nearby=ctx.on_nearby)
        else:
            ctx.worker = BlitzortungWsWorker(config, ctx.state, on_nearby=ctx.on_nearby)
        ctx.worker.start()
        # Reprise du mode 24/7 s'il était actif → l'archive se rouvre seule au démarrage.
        if config.ops.continuous_mode:
            ctx.set_continuous(True)
        pump_task = asyncio.create_task(mqtt_pump(), name="mqtt_pump")
        cells_task = asyncio.create_task(cells_loop(), name="cells_loop")
        try:
            yield
        finally:
            pump_task.cancel()
            cells_task.cancel()
            for t in (pump_task, cells_task):
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
            if ctx.worker is not None:
                ctx.worker.stop()
            if ctx.archive is not None:
                ctx.archive.close()
            ctx.db.close()

    app = FastAPI(title="Blitzortung Monitor", version="0.2.0", lifespan=lifespan)
    app.state.ctx = ctx

    # ── routes statiques ────────────────────────────────────────────────────
    if WEB_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    def _serve_html(filename: str, assets: tuple[str, ...]) -> HTMLResponse:
        path = WEB_DIR / filename
        if not path.exists():
            raise HTTPException(status_code=404, detail="Page introuvable")
        html = path.read_text(encoding="utf-8")
        # Cache-busting : on suffixe les assets locaux d'un querystring basé sur leur mtime,
        # pour que le navigateur recharge automatiquement après chaque édition.
        for asset in assets:
            apath = WEB_DIR / asset
            if apath.exists():
                v = int(apath.stat().st_mtime)
                html = html.replace(f"/static/{asset}", f"/static/{asset}?v={v}")
        return HTMLResponse(content=html, headers={"Cache-Control": "no-store, must-revalidate"})

    @app.get("/", include_in_schema=False)
    async def index() -> HTMLResponse:
        return _serve_html("index.html", ("app.js", "style.css"))

    @app.get("/how-it-works", include_in_schema=False)
    async def how_it_works() -> HTMLResponse:
        return _serve_html("how-it-works.html", ("how-it-works.css", "how-it-works.js", "style.css"))

    @app.get("/ops", include_in_schema=False)
    async def ops_page() -> HTMLResponse:
        return _serve_html("ops.html", ("ops.css", "ops.js", "style.css"))

    # ── API REST ────────────────────────────────────────────────────────────
    @app.get("/api/stats")
    async def get_stats() -> dict:
        snap = ctx.live_stats()
        snap["home"] = {"lat": config.home.lat, "lon": config.home.lon}
        snap["max_distance_km"] = config.filter.max_distance_km
        snap["alert_distance_km"] = config.filter.alert_distance_km
        snap["server_time"] = time.time()
        snap["run_id"] = ctx.run_id
        snap["source_type"] = config.source.type
        snap["continuous_mode"] = config.ops.continuous_mode
        snap["probe_est_s"] = (
            len(config.source.ws_endpoints) * config.source.ws_probe_seconds
            if config.source.type == "blitzortung_ws" else 2
        )
        return snap

    @app.get("/api/strikes/live")
    async def get_live(since: float = Query(0.0, description="timestamp unix")) -> dict:
        items = ctx.state.snapshot_recent_since(since)
        return {"strikes": [s.to_dict() for s in items]}

    @app.get("/api/strikes/history")
    async def get_history(
        date_from: str | None = Query(None, alias="from"),
        date_to: str | None = Query(None, alias="to"),
        max_distance: float | None = None,
        min_mds: int | None = None,
        limit: int = Query(100_000, le=500_000),
        decimate: bool = Query(True, description="agrège sur grille au-delà du seuil"),
    ) -> dict:
        frm, to = _iso_to_unix(date_from), _iso_to_unix(date_to)
        total = await run_in_threadpool(ctx.db.count_range, frm, to, max_distance, min_mds)
        bounds = await run_in_threadpool(ctx.db.date_bounds)
        box = {"min_unix": bounds[0], "max_unix": bounds[1]}
        # Au-delà du seuil, on renvoie une grille agrégée (≈ HIST_TARGET_POINTS points)
        # plutôt que des centaines de milliers de lignes : le navigateur reste fluide.
        if decimate and total > HIST_DECIMATE_THRESHOLD:
            grid = _hist_grid_deg(total)
            rows = await run_in_threadpool(
                ctx.db.query_range_decimated, grid, frm, to, max_distance, min_mds, HIST_TARGET_POINTS,
            )
            return {
                "strikes": rows, "aggregated": True, "grid_deg": grid,
                "total": total, "shown": len(rows), "bounds": box,
            }
        rows = await run_in_threadpool(
            ctx.db.query_range, from_unix=frm, to_unix=to,
            max_distance_km=max_distance, min_mds=min_mds, limit=limit,
        )
        return {
            "strikes": rows, "aggregated": False,
            "total": total, "shown": len(rows), "bounds": box,
        }

    @app.get("/api/history/per_hour")
    async def get_per_hour(days: int = Query(30, ge=1, le=3650)) -> dict:
        return {"per_hour": await run_in_threadpool(ctx.db.count_per_hour, days)}

    @app.get("/api/cells")
    async def get_cells() -> dict:
        return {"cells": [c.to_dict() for c in ctx.state.snapshot_cells()]}

    @app.get("/api/source/probe")
    async def source_probe() -> dict:
        """Mesure la latence de chaque endpoint Blitzortung et renvoie le classement."""
        res = await run_in_threadpool(
            probe_endpoints, config.source.ws_endpoints, config.source.ws_probe_seconds
        )
        return {"endpoints": res, "current": ctx.state.snapshot_stats().get("source")}

    # ── Vague 3 : vérification, analytics, catalogue, exports ─────────────────
    @app.get("/api/verification")
    async def get_verification(days: int = Query(7, ge=1, le=3650)) -> dict:
        now2 = time.time()
        frm = now2 - days * 86400
        ring = config.filter.alert_distance_km
        preds = await run_in_threadpool(ctx.db.predictions_between, frm, now2)
        in_ring = await run_in_threadpool(ctx.db.strike_ts_in_ring, ring, frm, now2)
        report = evaluate(
            preds, in_ring,
            tolerance_min=config.predict.verify_tolerance_min,
            gap_min=config.predict.arrival_gap_min,
        )
        report["ring_km"] = ring
        report["days"] = days
        return report

    @app.get("/api/analytics/summary")
    async def get_analytics(days: int = Query(365, ge=1, le=3650)) -> dict:
        hod = await run_in_threadpool(ctx.db.count_by_hour_of_day, days)
        wd = await run_in_threadpool(ctx.db.count_by_weekday, days)
        rose = await run_in_threadpool(ctx.db.bearing_rose, days)
        dist = await run_in_threadpool(
            ctx.db.distance_histogram, days, 10.0, config.filter.max_distance_km
        )
        card = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
                "S", "SSO", "SO", "OSO", "O", "ONO", "NO", "NNO"]
        hour = [0] * 24
        for r in hod:
            hour[r["hour"]] = r["n"]
        week = [0] * 7
        for r in wd:
            week[r["weekday"]] = r["n"]
        rose_arr = [0] * 16
        for r in rose:
            rose_arr[r["sector"]] = r["n"]
        return {
            "hour_of_day": hour,
            "weekday": week,
            "rose": [{"dir": card[i], "n": rose_arr[i]} for i in range(16)],
            "distance_hist": dist,
            "days": days,
        }

    @app.get("/api/cells/catalog")
    async def get_catalog(days: int = Query(30, ge=1, le=3650), min_strikes: int = 0) -> dict:
        frm = time.time() - days * 86400
        cells = await run_in_threadpool(ctx.db.list_cells, frm, None, min_strikes, 500)
        return {"cells": cells, "current_run": ctx.run_id}

    @app.get("/api/cells/track")
    async def get_cell_track(run_id: str, cell_id: int) -> dict:
        rows = await run_in_threadpool(ctx.db.cell_track, run_id, cell_id)
        return {"track": rows}

    def _export_rows(date_from, date_to, max_distance, min_mds, limit):
        return ctx.db.query_range(
            from_unix=_iso_to_unix(date_from), to_unix=_iso_to_unix(date_to),
            max_distance_km=max_distance, min_mds=min_mds, limit=limit,
        )

    @app.get("/api/export/strikes.csv")
    async def export_csv(
        date_from: str | None = Query(None, alias="from"),
        date_to: str | None = Query(None, alias="to"),
        max_distance: float | None = None, min_mds: int | None = None,
        limit: int = Query(500_000, le=2_000_000),
    ) -> Response:
        rows = await run_in_threadpool(_export_rows, date_from, date_to, max_distance, min_mds, limit)
        buf = io.StringIO()
        w = csv.writer(buf)
        cols = ["ts_unix", "ts_utc", "lat", "lon", "distance_km", "bearing_deg", "mds", "home_lat", "home_lon"]
        w.writerow(cols)
        for r in rows:
            w.writerow([r.get(c) for c in cols])
        return Response(
            content=buf.getvalue(), media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=strikes.csv"},
        )

    @app.get("/api/export/strikes.geojson")
    async def export_geojson(
        date_from: str | None = Query(None, alias="from"),
        date_to: str | None = Query(None, alias="to"),
        max_distance: float | None = None, min_mds: int | None = None,
        limit: int = Query(500_000, le=2_000_000),
    ) -> Response:
        rows = await run_in_threadpool(_export_rows, date_from, date_to, max_distance, min_mds, limit)
        feats = [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
                "properties": {k: r.get(k) for k in ("ts_unix", "ts_utc", "distance_km", "bearing_deg", "mds")},
            }
            for r in rows
        ]
        return JSONResponse(
            {"type": "FeatureCollection", "features": feats},
            headers={"Content-Disposition": "attachment; filename=strikes.geojson"},
        )

    @app.get("/api/export/cell_tracks.geojson")
    async def export_cell_tracks(run_id: str | None = None) -> Response:
        rid = run_id or ctx.run_id
        cells = await run_in_threadpool(ctx.db.list_cells, None, None, 0, 5000)
        feats = []
        for c in cells:
            if c["run_id"] != rid:
                continue
            tr = await run_in_threadpool(ctx.db.cell_track, rid, c["cell_id"])
            if len(tr) < 2:
                continue
            feats.append({
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": [[t["lon"], t["lat"]] for t in tr]},
                "properties": {
                    "cell_id": c["cell_id"], "max_severity": c["max_severity"],
                    "total_strikes": c["total_strikes"], "peak_flash_rate": c["peak_flash_rate"],
                },
            })
        return JSONResponse(
            {"type": "FeatureCollection", "features": feats},
            headers={"Content-Disposition": f"attachment; filename=cell_tracks_{rid}.geojson"},
        )

    # ── Vague 4 : géocodage à la demande + déplacement de HOME ────────────────
    @app.get("/api/geocode")
    async def geocode_ep(lat: float, lon: float) -> dict:
        key = f"{round(lat, 2)},{round(lon, 2)}"   # cache ~1 km
        cached = await run_in_threadpool(ctx.db.geocode_get, key)
        if cached is not None:
            return {"name": cached, "cached": True}
        name = await reverse_nominatim(lat, lon)
        if name:
            await run_in_threadpool(ctx.db.geocode_put, key, name)
        return {"name": name, "cached": False}

    @app.post("/api/home")
    async def set_home(home: HomeIn) -> dict:
        if not (-90 <= home.lat <= 90 and -180 <= home.lon <= 180):
            raise HTTPException(status_code=400, detail="Coordonnées hors limites")
        # Effet immédiat : le worker MQTT et l'analyse partagent ce même objet config.
        config.home.lat = home.lat
        config.home.lon = home.lon
        persisted = await run_in_threadpool(update_home, config.source_path, home.lat, home.lon)
        return {"ok": True, "home": {"lat": home.lat, "lon": home.lon}, "persisted": persisted}

    def _config_snapshot() -> dict:
        return {
            "home": {"lat": config.home.lat, "lon": config.home.lon},
            "max_distance_km": config.filter.max_distance_km,
            "alert_distance_km": config.filter.alert_distance_km,
            "cluster_eps_km": config.analysis.cluster_eps_km,
            "cluster_min_samples": config.analysis.cluster_min_samples,
            "cell_window_minutes": config.analysis.cell_window_minutes,
            "min_mds_quality": config.analysis.min_mds_quality,
            "tick_seconds": config.analysis.tick_seconds,
            "strike_ring_km": config.analysis.strike_ring_km,
            "source_type": config.source.type,
        }

    @app.get("/api/config")
    async def get_config() -> dict:
        return _config_snapshot()

    @app.post("/api/config")
    async def set_config(cfg_in: ConfigIn) -> dict:
        """Applique des paramètres à chaud (worker + analyse lisent la config en direct)
        et les persiste dans config.toml."""
        sv: dict[str, dict[str, object]] = {}
        if cfg_in.home_lat is not None or cfg_in.home_lon is not None:
            lat = cfg_in.home_lat if cfg_in.home_lat is not None else config.home.lat
            lon = cfg_in.home_lon if cfg_in.home_lon is not None else config.home.lon
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise HTTPException(status_code=400, detail="Coordonnées hors limites")
            config.home.lat, config.home.lon = lat, lon
            sv["home"] = {"lat": lat, "lon": lon}
        if cfg_in.max_distance_km is not None:
            config.filter.max_distance_km = cfg_in.max_distance_km
            sv.setdefault("filter", {})["max_distance_km"] = cfg_in.max_distance_km
        if cfg_in.alert_distance_km is not None:
            config.filter.alert_distance_km = cfg_in.alert_distance_km
            sv.setdefault("filter", {})["alert_distance_km"] = cfg_in.alert_distance_km
        if cfg_in.cluster_eps_km is not None:
            config.analysis.cluster_eps_km = cfg_in.cluster_eps_km
            sv.setdefault("analysis", {})["cluster_eps_km"] = cfg_in.cluster_eps_km
        if cfg_in.cluster_min_samples is not None:
            config.analysis.cluster_min_samples = int(cfg_in.cluster_min_samples)
            sv.setdefault("analysis", {})["cluster_min_samples"] = int(cfg_in.cluster_min_samples)
        if cfg_in.cell_window_minutes is not None:
            config.analysis.cell_window_minutes = int(cfg_in.cell_window_minutes)
            sv.setdefault("analysis", {})["cell_window_minutes"] = int(cfg_in.cell_window_minutes)
        if cfg_in.min_mds_quality is not None:
            config.analysis.min_mds_quality = int(cfg_in.min_mds_quality)
            sv.setdefault("analysis", {})["min_mds_quality"] = int(cfg_in.min_mds_quality)
        if cfg_in.tick_seconds is not None:
            config.analysis.tick_seconds = max(1, int(cfg_in.tick_seconds))
            sv.setdefault("analysis", {})["tick_seconds"] = config.analysis.tick_seconds
        if cfg_in.strike_ring_km is not None:
            config.analysis.strike_ring_km = cfg_in.strike_ring_km
            sv.setdefault("analysis", {})["strike_ring_km"] = cfg_in.strike_ring_km
        # Si le rayon ou HOME change, on borne immédiatement le système à l'anneau :
        # purge des impacts hors-zone + des cellules dont le centroïde est dehors.
        if "home" in sv or ("filter" in sv and "max_distance_km" in sv["filter"]):
            max_d = config.filter.max_distance_km
            ctx.state.prune_beyond(max_d)
            with ctx.state.lock:
                ctx.state.cells = {
                    cid: c for cid, c in ctx.state.cells.items()
                    if haversine(config.home.lat, config.home.lon, c.centroid_lat, c.centroid_lon) <= max_d
                }

        persisted = await run_in_threadpool(update_config, config.source_path, sv) if sv else False
        return {"ok": True, "persisted": persisted, "config": _config_snapshot()}

    # ── Mode 24/7 : supervision & contrôles ──────────────────────────────────
    @app.get("/api/ops/status")
    async def ops_status() -> dict:
        snap = ctx.live_stats()
        storage = await run_in_threadpool(ctx.db.storage_info)
        cap24 = await run_in_threadpool(ctx.db.count_range, time.time() - 86400, None, None, None)
        archive: dict = {"active": ctx.archive is not None, "path": str(ctx.archive_path())}
        if ctx.archive is not None:
            a_store = await run_in_threadpool(ctx.archive.storage_info)
            archive["db_bytes"] = a_store["db_bytes"]
            archive["wal_bytes"] = a_store["wal_bytes"]
            archive["total"] = await run_in_threadpool(ctx.archive.count)
            archive["started_at"] = ctx.archive_started_at
        return {
            "archive": archive,
            "continuous_mode": config.ops.continuous_mode,
            "source": snap.get("source"), "source_type": config.source.type,
            "endpoint": getattr(ctx.worker, "endpoint", None),
            "latency_s": snap.get("latency_s"),
            "started_at": snap.get("started_at"), "server_time": time.time(),
            "last_message_at": snap.get("last_message_at"),
            "mqtt_connected": snap.get("mqtt_connected"),
            "world_per_s": snap.get("world_per_s"), "nearby_per_min": snap.get("nearby_per_min"),
            "queue_dropped": snap.get("queue_dropped"),
            "queue_depth": snap.get("queue_depth"), "queue_max": snap.get("queue_max"),
            "cells_count": snap.get("cells_count"), "cells_compute_ms": snap.get("cells_compute_ms"),
            "recent_buffer": snap.get("recent_buffer"), "recent_buffer_max": snap.get("recent_buffer_max"),
            "logged_total": snap.get("logged_total"), "logged_session": snap.get("logged_session"),
            "capture_24h": cap24,
            "retention_days": ctx.db.retention_days,
            "storage": storage,
            "can_reprobe": hasattr(ctx.worker, "request_reselect"),
        }

    @app.post("/api/ops/mode")
    async def ops_mode(m: ModeIn) -> dict:
        ctx.set_continuous(m.enabled)   # ouvre/ferme l'archive 24/7 dédiée
        persisted = await run_in_threadpool(
            update_config, config.source_path, {"ops": {"continuous_mode": config.ops.continuous_mode}}
        )
        return {
            "ok": True, "continuous_mode": config.ops.continuous_mode,
            "persisted": persisted, "archive_path": str(ctx.archive_path()),
        }

    @app.post("/api/ops/retention")
    async def ops_retention(r: RetentionIn) -> dict:
        days = max(0, int(r.days))
        ctx.db.retention_days = days
        persisted = await run_in_threadpool(
            update_config, config.source_path, {"db": {"retention_days": days}}
        )
        return {"ok": True, "retention_days": days, "persisted": persisted}

    @app.post("/api/ops/reprobe")
    async def ops_reprobe() -> dict:
        w = ctx.worker
        if w is not None and hasattr(w, "request_reselect"):
            w.request_reselect()
            return {"ok": True, "message": "Re-sélection d'endpoint demandée"}
        return {"ok": False, "message": "Indisponible pour cette source"}

    @app.post("/api/ops/maintain")
    async def ops_maintain() -> dict:
        res = await run_in_threadpool(ctx.db.maintain)
        return {"ok": True, **res}

    @app.post("/api/ops/backup")
    async def ops_backup() -> dict:
        stem = Path(config.db.path)
        dest = stem.with_name(f"{stem.stem}_backup_{int(time.time())}.db")
        if dest.exists():
            raise HTTPException(status_code=409, detail="Une sauvegarde de cet horodatage existe déjà")
        try:
            size = await run_in_threadpool(ctx.db.backup, str(dest))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Échec de la sauvegarde : {exc}") from None
        return {"ok": True, "path": str(dest), "bytes": size}

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        await ctx.hub.connect(ws)
        try:
            # Envoi initial : stats + cellules courantes
            await ws.send_json({"type": "stats", "data": ctx.live_stats()})
            await ws.send_json(
                {"type": "cells", "data": [c.to_dict() for c in ctx.state.snapshot_cells()]}
            )
            # Backfill : la fenêtre récente d'impacts pour que la carte ne soit pas vide.
            window_s = config.analysis.cell_window_minutes * 60
            recent = ctx.state.snapshot_recent_since(time.time() - window_s)
            await ws.send_json(
                {"type": "strikes_batch", "data": [s.to_dict() for s in recent[-5000:]]}
            )
            while True:
                # On garde la connexion ouverte ; client peut ping via texte arbitraire.
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            await ctx.hub.disconnect(ws)

    return app
