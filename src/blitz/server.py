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
from .config import Config, update_home
from .db import Database
from .geocode import reverse_nominatim
from .mqtt_worker import MqttWorker
from .state import SharedState, Strike
from .verification import evaluate


class HomeIn(BaseModel):
    lat: float
    lon: float

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent / "web"


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
        self.db = Database(Path(config.db.path))
        self.state.stats["logged_total"] = self.db.count()
        self.hub = Hub()
        self.worker: MqttWorker | None = None
        self._next_cell_id = 1
        # ── Vague 3 : mémoire & vérification ─────────────────────────────────
        self.run_id = uuid.uuid4().hex[:12]   # les cell_id repartent à 1 à chaque run
        self._persisted_track_ts: dict[int, float] = {}
        self._open_warnings: set[int] = set()

    def on_nearby(self, s: Strike) -> None:
        """Listener appelé par le worker MQTT pour chaque impact dans la zone."""
        self.db.insert_strike(
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
            await asyncio.sleep(config.analysis.tick_seconds)
            try:
                with ctx.state.lock:
                    recent = list(ctx.state.recent)
                    previous = dict(ctx.state.cells)

                def _recompute(recent=recent, previous=previous) -> tuple[dict, int]:
                    windowed = filter_window(recent, config.analysis.cell_window_minutes)
                    return update_cells(
                        config.home, windowed, previous, config.analysis,
                        next_id=ctx._next_cell_id,
                    )

                cells, next_id = await loop.run_in_executor(None, _recompute)
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
            if eta_s is not None and prob >= min_prob:
                current_warn.add(cid)
                if cid not in ctx._open_warnings:   # nouvelle alerte → on la journalise une fois
                    new_preds.append({
                        "run_id": ctx.run_id, "cell_id": cid, "ts_made": now2,
                        "eta": eta_s, "pa": now2 + eta_s * 60.0,
                        "prob": prob, "closest": cd.get("closest_approach_km"),
                    })

        ctx.db.upsert_cells(ctx.run_id, live_dicts)
        ctx.db.insert_cell_tracks(ctx.run_id, track_rows)
        ctx.db.log_predictions(new_preds)
        ctx._open_warnings = current_warn

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        ctx.worker = MqttWorker(config, ctx.state, on_nearby=ctx.on_nearby)
        ctx.worker.start()
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
            ctx.db.close()

    app = FastAPI(title="Blitzortung Monitor", version="0.2.0", lifespan=lifespan)
    app.state.ctx = ctx

    # ── routes statiques ────────────────────────────────────────────────────
    if WEB_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> HTMLResponse:
        path = WEB_DIR / "index.html"
        if not path.exists():
            raise HTTPException(status_code=404, detail="Frontend introuvable")
        html = path.read_text(encoding="utf-8")
        # Cache-busting : on suffixe les assets locaux d'un querystring basé sur leur mtime,
        # pour que le navigateur recharge automatiquement après chaque édition.
        for asset in ("app.js", "style.css"):
            apath = WEB_DIR / asset
            if apath.exists():
                v = int(apath.stat().st_mtime)
                html = html.replace(f"/static/{asset}", f"/static/{asset}?v={v}")
        return HTMLResponse(
            content=html,
            headers={"Cache-Control": "no-store, must-revalidate"},
        )

    # ── API REST ────────────────────────────────────────────────────────────
    @app.get("/api/stats")
    async def get_stats() -> dict:
        snap = ctx.state.snapshot_stats()
        snap["home"] = {"lat": config.home.lat, "lon": config.home.lon}
        snap["max_distance_km"] = config.filter.max_distance_km
        snap["alert_distance_km"] = config.filter.alert_distance_km
        snap["server_time"] = time.time()
        snap["run_id"] = ctx.run_id
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
    ) -> dict:
        rows = await run_in_threadpool(
            ctx.db.query_range,
            from_unix=_iso_to_unix(date_from),
            to_unix=_iso_to_unix(date_to),
            max_distance_km=max_distance,
            min_mds=min_mds,
            limit=limit,
        )
        bounds = await run_in_threadpool(ctx.db.date_bounds)
        return {"strikes": rows, "bounds": {"min_unix": bounds[0], "max_unix": bounds[1]}}

    @app.get("/api/history/per_hour")
    async def get_per_hour(days: int = Query(30, ge=1, le=3650)) -> dict:
        return {"per_hour": await run_in_threadpool(ctx.db.count_per_hour, days)}

    @app.get("/api/cells")
    async def get_cells() -> dict:
        return {"cells": [c.to_dict() for c in ctx.state.snapshot_cells()]}

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

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        await ctx.hub.connect(ws)
        try:
            # Envoi initial : stats + cellules courantes
            await ws.send_json({"type": "stats", "data": ctx.state.snapshot_stats()})
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
