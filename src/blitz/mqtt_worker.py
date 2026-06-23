"""Worker MQTT robuste : reconnexion automatique + queue thread-safe."""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from typing import Callable, Optional

import paho.mqtt.client as mqtt

from .config import Config
from .geo import bearing, haversine
from .state import SharedState, Strike

logger = logging.getLogger(__name__)


# Callback appelé pour chaque impact dans la zone (utile pour la persistance DB).
StrikeListener = Callable[[Strike], None]


class MqttWorker:
    """Encapsule le client paho-mqtt en thread d'arrière-plan."""

    def __init__(
        self,
        config: Config,
        state: SharedState,
        on_nearby: Optional[StrikeListener] = None,
    ) -> None:
        self.cfg = config
        self.state = state
        self.on_nearby = on_nearby
        self.queue: queue.Queue[Strike] = queue.Queue(maxsize=10_000)
        self._stop = threading.Event()
        self._client: mqtt.Client | None = None

    # ── construction ────────────────────────────────────────────────────────
    def _new_client(self) -> mqtt.Client:
        try:
            client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION1)
        except AttributeError:
            client = mqtt.Client()  # paho-mqtt 1.x
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.reconnect_delay_set(
            min_delay=self.cfg.mqtt.reconnect_min_s,
            max_delay=self.cfg.mqtt.reconnect_max_s,
        )
        return client

    # ── callbacks ───────────────────────────────────────────────────────────
    def _on_connect(self, client: mqtt.Client, userdata, flags, rc, *args) -> None:
        if rc == 0:
            self.state.set_mqtt_connected(True)
            self.state.set_source(f"mqtt:{self.cfg.mqtt.host}")
            logger.info("Connecté à %s:%s — abonnement à %s",
                        self.cfg.mqtt.host, self.cfg.mqtt.port, self.cfg.mqtt.topic)
            client.subscribe(self.cfg.mqtt.topic)
        else:
            logger.error("Échec connexion MQTT (rc=%s)", rc)

    def _on_disconnect(self, client: mqtt.Client, userdata, rc, *args) -> None:
        self.state.set_mqtt_connected(False)
        if rc != 0:
            logger.warning("Déconnexion inattendue (rc=%s) — paho va reconnecter", rc)
        else:
            logger.info("Déconnexion propre")

    def _on_message(self, client: mqtt.Client, userdata, msg: mqtt.MQTTMessage) -> None:
        try:
            data = json.loads(msg.payload.decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            return

        lat, lon = data.get("lat"), data.get("lon")
        if lat is None or lon is None:
            return

        self.state.bump_world()

        ts = data.get("time", 0) / 1e9
        if ts > 0:
            self.state.record_latency(time.time() - ts, None)

        dist = haversine(self.cfg.home.lat, self.cfg.home.lon, lat, lon)
        if dist > self.cfg.filter.max_distance_km:
            return

        brg = bearing(self.cfg.home.lat, self.cfg.home.lon, lat, lon)
        sig = data.get("sig")
        if isinstance(sig, list):
            mds: int | None = len(sig)
        elif isinstance(data.get("mds"), (int, float)):
            mds = int(data["mds"])
        else:
            mds = None

        strike = Strike(
            ts_unix=ts,
            lat=float(lat),
            lon=float(lon),
            distance_km=dist,
            bearing_deg=brg,
            mds=mds,
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
            logger.warning("Queue MQTT pleine — drop d'un impact")

    # ── cycle de vie ────────────────────────────────────────────────────────
    def start(self) -> None:
        self._client = self._new_client()
        self._client.connect_async(
            self.cfg.mqtt.host,
            self.cfg.mqtt.port,
            keepalive=self.cfg.mqtt.keepalive_s,
        )
        self._client.loop_start()
        logger.info("Worker MQTT démarré")

    def stop(self) -> None:
        self._stop.set()
        if self._client is not None:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:
                logger.exception("Erreur à l'arrêt du client MQTT")
        logger.info("Worker MQTT arrêté")
