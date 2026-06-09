"""Serveur FastAPI : REST + WebSocket + worker MQTT en arrière-plan."""

from __future__ import annotations

import asyncio
import logging
import queue
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .analysis import filter_window, update_cells
from .config import Config
from .db import Database
from .mqtt_worker import MqttWorker
from .state import SharedState, Strike

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
        self.state = SharedState()
        self.db = Database(Path(config.db.path))
        self.state.stats["logged_total"] = self.db.count()
        self.hub = Hub()
        self.worker: MqttWorker | None = None
        self._next_cell_id = 1

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
        raise HTTPException(status_code=400, detail=f"Date invalide : {s}")


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
        """Recalcule les cellules à intervalle régulier et notifie les WS."""
        while True:
            await asyncio.sleep(config.analysis.tick_seconds)
            try:
                with ctx.state.lock:
                    recent = list(ctx.state.recent)
                recent = filter_window(recent, config.analysis.cell_window_minutes)
                with ctx.state.lock:
                    previous = dict(ctx.state.cells)
                cells, next_id = update_cells(
                    config.home, recent, previous, config.analysis, next_id=ctx._next_cell_id
                )
                ctx._next_cell_id = next_id
                with ctx.state.lock:
                    ctx.state.cells = cells
                await ctx.hub.broadcast(
                    {"type": "cells", "data": [c.to_dict() for c in cells.values()]}
                )
            except Exception:
                logger.exception("Erreur dans la boucle d'analyse")

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
        rows = ctx.db.query_range(
            from_unix=_iso_to_unix(date_from),
            to_unix=_iso_to_unix(date_to),
            max_distance_km=max_distance,
            min_mds=min_mds,
            limit=limit,
        )
        bounds = ctx.db.date_bounds()
        return {"strikes": rows, "bounds": {"min_unix": bounds[0], "max_unix": bounds[1]}}

    @app.get("/api/history/per_hour")
    async def get_per_hour(days: int = Query(30, ge=1, le=3650)) -> dict:
        return {"per_hour": ctx.db.count_per_hour(days)}

    @app.get("/api/cells")
    async def get_cells() -> dict:
        return {"cells": [c.to_dict() for c in ctx.state.snapshot_cells()]}

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        await ctx.hub.connect(ws)
        try:
            # Envoi initial : stats + cellules courantes
            await ws.send_json({"type": "stats", "data": ctx.state.snapshot_stats()})
            await ws.send_json(
                {"type": "cells", "data": [c.to_dict() for c in ctx.state.snapshot_cells()]}
            )
            while True:
                # On garde la connexion ouverte ; client peut ping via texte arbitraire.
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            await ctx.hub.disconnect(ws)

    return app
