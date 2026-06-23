"""Flux direct depuis les serveurs WebSocket de Blitzortung (faible latence).

Le broker MQTT communautaire republie les impacts avec ~1-2 min de retard ; les
serveurs WS officiels (``wss://wsN.blitzortung.org/``) les poussent en quelques
secondes. Les messages sont compressés par un LZW maison qu'on décode ici.

Au démarrage on sonde plusieurs endpoints et on garde celui de plus faible
latence (cf. `probe_endpoints`).
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from typing import Callable, Optional

from .config import Config
from .geo import bearing, haversine
from .state import SharedState, Strike

logger = logging.getLogger(__name__)

StrikeListener = Callable[[Strike], None]

_HANDSHAKE = '{"a":111}'


# ─── Décodage du flux Blitzortung ────────────────────────────────────────────
def decode(s: str) -> str:
    """Décompression LZW utilisée par maps.blitzortung.org (port du JS officiel)."""
    if not s:
        return s
    d = list(s)
    e: dict[int, str] = {}
    c = d[0]
    f = c
    out = [c]
    h = 256
    o = h
    for i in range(1, len(d)):
        code = ord(d[i])
        if h > code:
            a = d[i]
        elif code in e:
            a = e[code]
        else:
            a = f + c
        out.append(a)
        c = a[0]
        e[o] = f + c
        o += 1
        f = a
    return "".join(out)


def parse_message(raw) -> Optional[dict]:
    """Renvoie le dict JSON d'un message (déjà JSON ou compressé), sinon None."""
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8", "ignore")
        except Exception:
            return None
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        pass
    try:
        obj = json.loads(decode(raw))
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def parse_strike(data: dict, home, now: float) -> Optional[dict]:
    """Extrait les champs utiles d'un impact Blitzortung décodé."""
    lat, lon = data.get("lat"), data.get("lon")
    if lat is None or lon is None:
        return None
    ts = data.get("time", 0) / 1e9
    sig = data.get("sig")
    if isinstance(sig, list):
        mds: int | None = len(sig)
    elif isinstance(data.get("mds"), (int, float)):
        mds = int(data["mds"])
    else:
        mds = None
    delay = data.get("delay")
    delay = float(delay) if isinstance(delay, (int, float)) else None
    return {
        "lat": float(lat),
        "lon": float(lon),
        "ts": ts,
        "mds": mds,
        "delay": delay,
        "dist": haversine(home.lat, home.lon, lat, lon),
        "bearing": bearing(home.lat, home.lon, lat, lon),
        "latency": (now - ts) if ts > 0 else None,
    }


# ─── Sondage de latence des endpoints ────────────────────────────────────────
def _probe_one(host: str, seconds: float) -> tuple[float | None, int]:
    """Mesure la latence médiane (s) d'un endpoint sur `seconds`. (None, 0) si KO."""
    try:
        import websocket
    except ImportError:
        logger.error("websocket-client absent : flux Blitzortung WS indisponible")
        return None, 0
    url = f"wss://{host}.blitzortung.org/"
    lat: list[float] = []
    try:
        ws = websocket.create_connection(url, timeout=6)
        ws.send(_HANDSHAKE)
        ws.settimeout(seconds)
        end = time.time() + seconds
        while time.time() < end:
            try:
                msg = ws.recv()
            except Exception:
                break
            data = parse_message(msg)
            if isinstance(data, dict):
                t = data.get("time", 0) / 1e9
                if t > 0:
                    lat.append(time.time() - t)
        ws.close()
    except Exception as exc:
        logger.debug("probe %s: %s", host, exc)
        return None, 0
    if not lat:
        return None, 0
    lat.sort()
    return round(lat[len(lat) // 2], 1), len(lat)


def probe_endpoints(hosts: list[str], seconds: float) -> list[dict]:
    """Sonde chaque endpoint et renvoie [{endpoint, latency_s, n}] trié par latence."""
    out = []
    for h in hosts:
        med, n = _probe_one(h, seconds)
        out.append({"endpoint": h, "latency_s": med, "n": n})
    out.sort(key=lambda r: (r["latency_s"] is None, r["latency_s"] if r["latency_s"] is not None else 1e9))
    return out


# ─── Worker ──────────────────────────────────────────────────────────────────
class BlitzortungWsWorker:
    """Flux WebSocket direct, en thread, avec sélection d'endpoint et reconnexion."""

    def __init__(self, config: Config, state: SharedState, on_nearby: Optional[StrikeListener] = None) -> None:
        self.cfg = config
        self.state = state
        self.on_nearby = on_nearby
        self.queue: queue.Queue[Strike] = queue.Queue(maxsize=10_000)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ws = None
        self.endpoint: str | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="bt-ws", daemon=True)
        self._thread.start()
        logger.info("Worker Blitzortung WS démarré")

    def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass
        logger.info("Worker Blitzortung WS arrêté")

    def _select_endpoint(self) -> str | None:
        hosts = self.cfg.source.ws_endpoints or ["ws1"]
        results = probe_endpoints(hosts, self.cfg.source.ws_probe_seconds)
        for r in results:
            logger.info("probe %s: latence=%s n=%s", r["endpoint"], r["latency_s"], r["n"])
        best = next((r for r in results if r["latency_s"] is not None), None)
        if best is None:
            logger.warning("Aucun endpoint Blitzortung joignable — repli sur %s", hosts[0])
            return hosts[0]
        logger.info("Endpoint Blitzortung retenu : %s (%.1fs)", best["endpoint"], best["latency_s"])
        return best["endpoint"]

    def _run(self) -> None:
        try:
            import websocket  # noqa: F401
        except ImportError:
            logger.error("websocket-client absent : installez-le ou passez [source].type='mqtt'")
            return
        self.endpoint = self._select_endpoint()
        while not self._stop.is_set():
            try:
                self._stream(self.endpoint)
            except Exception:
                if not self._stop.is_set():
                    logger.warning("Flux Blitzortung interrompu — reconnexion dans 3 s", exc_info=True)
            if self._stop.is_set():
                break
            time.sleep(3)

    def _stream(self, host: str) -> None:
        import websocket
        url = f"wss://{host}.blitzortung.org/"
        ws = websocket.create_connection(url, timeout=8)
        self._ws = ws
        ws.send(_HANDSHAKE)
        ws.settimeout(45)
        self.state.set_mqtt_connected(True)
        self.state.set_source(f"{host}.blitzortung.org")
        logger.info("Connecté à %s", url)
        try:
            while not self._stop.is_set():
                msg = ws.recv()
                if not msg:
                    continue
                data = parse_message(msg)
                if isinstance(data, dict):
                    self._handle(data)
        finally:
            self.state.set_mqtt_connected(False)
            try:
                ws.close()
            except Exception:
                pass

    def _handle(self, data: dict) -> None:
        p = parse_strike(data, self.cfg.home, time.time())
        if p is None:
            return
        self.state.bump_world()
        if p["latency"] is not None:
            self.state.record_latency(p["latency"], p["delay"])
        if p["dist"] > self.cfg.filter.max_distance_km:
            return
        strike = Strike(
            ts_unix=p["ts"], lat=p["lat"], lon=p["lon"],
            distance_km=p["dist"], bearing_deg=p["bearing"], mds=p["mds"],
        )
        self.state.add_strike(strike)
        if self.on_nearby is not None:
            try:
                self.on_nearby(strike)
            except Exception:
                logger.exception("Listener on_nearby a levé une exception")
        try:
            self.queue.put_nowait(strike)
        except queue.Full:
            self.state.record_drop()
            logger.warning("Queue pleine — drop d'un impact")
