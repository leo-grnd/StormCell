"""
⚡ Blitzortung Lightning Monitor — prototype
─────────────────────────────────────────────
Connecte au broker MQTT communautaire de Blitzortung, filtre les impacts
par distance depuis ta position, affiche un tableau temps réel et
enregistre chaque impact dans une base SQLite locale (append sur restart).

Dépendances :
    pip install paho-mqtt rich

Lancement :
    python blitzortung_monitor.py

La base lightning_log.db est créée au premier lancement à côté du script.
Ctrl+C pour quitter (fermeture propre de la DB).

Note: usage privé / divertissement uniquement, conformément à la
politique d'utilisation de Blitzortung.org.
"""

import json
import math
import sqlite3
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import paho.mqtt.client as mqtt
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table


# ─── CONFIGURATION ──────────────────────────────────────────────────────────
HOME_LAT = 44.243318            # Mondragon — remplace par ta position
HOME_LON = 4.716102
MAX_DISTANCE_KM = 100         # n'affiche que les impacts dans ce rayon
MAX_STRIKES_SHOWN = 15        # nombre de lignes du tableau

DB_PATH = Path("lightning_log.db")   # base SQLite (créée à côté du script)

BROKER_HOST = "blitzortung.ha.sed.pl"
BROKER_PORT = 1883
TOPIC = "blitzortung/1.1/#"   # tous les impacts (filtrés ensuite côté client)


# ─── CALCULS PHYSIQUES & GÉO ────────────────────────────────────────────────
EARTH_RADIUS_KM = 6371.0
SPEED_OF_SOUND_MS = 340.0     # m/s à ~15 °C — bonne approximation

def haversine(lat1, lon1, lat2, lon2):
    """Distance en km entre deux points (formule de haversine)."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))

def bearing(lat1, lon1, lat2, lon2):
    """Azimut (degrés, 0 = Nord, sens horaire) depuis point1 vers point2."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlmb = math.radians(lon2 - lon1)
    y = math.sin(dlmb) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlmb)
    return (math.degrees(math.atan2(y, x)) + 360) % 360

def cardinal(deg):
    """Conversion azimut → point cardinal (16 directions)."""
    dirs = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSO", "SO", "OSO", "O", "ONO", "NO", "NNO"]
    return dirs[int((deg + 11.25) // 22.5) % 16]

def color_for_distance(d_km):
    """Code couleur selon la proximité — utile pour l'affichage."""
    if d_km < 15:   return "bold red"
    if d_km < 30:  return "orange3"
    if d_km < 250:  return "yellow"
    return "green"


# ─── ÉTAT PARTAGÉ ───────────────────────────────────────────────────────────
strikes = deque(maxlen=MAX_STRIKES_SHOWN)
stats = {
    "total": 0,           # tous les impacts mondiaux reçus (cette session)
    "nearby": 0,          # impacts dans le rayon (cette session)
    "closest": None,
    "started": time.time(),
    "logged_session": 0,  # lignes ajoutées à la DB cette session
    "logged_total": 0,    # total cumulé en DB (toutes sessions)
}
console = Console()
db_conn: sqlite3.Connection | None = None


# ─── BASE DE DONNÉES ────────────────────────────────────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS strikes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_unix     REAL    NOT NULL,   -- timestamp Unix (secondes, float)
    ts_utc      TEXT    NOT NULL,   -- ISO 8601 lisible
    lat         REAL    NOT NULL,
    lon         REAL    NOT NULL,
    distance_km REAL    NOT NULL,   -- depuis HOME_LAT/HOME_LON au moment du log
    bearing_deg REAL    NOT NULL,
    mds         INTEGER,            -- nombre de stations détectrices
    home_lat    REAL    NOT NULL,   -- pour pouvoir retraiter si HOME change
    home_lon    REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_strikes_ts   ON strikes(ts_unix);
CREATE INDEX IF NOT EXISTS idx_strikes_dist ON strikes(distance_km);
"""

def init_db():
    """Ouvre/crée la base, applique le schéma, renvoie le total déjà stocké."""
    global db_conn
    db_conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    db_conn.execute("PRAGMA journal_mode=WAL")     # lecture pendant l'écriture
    db_conn.execute("PRAGMA synchronous=NORMAL")   # bon compromis perf/safety
    db_conn.executescript(SCHEMA)
    db_conn.commit()
    total = db_conn.execute("SELECT COUNT(*) FROM strikes").fetchone()[0]
    return total

def log_strike(ts_unix, lat, lon, distance_km, bearing_deg, mds):
    """Insère un impact dans la base et incrémente les compteurs."""
    if db_conn is None:
        return
    try:
        mds_int = int(mds) if isinstance(mds, (int, float)) else None
        ts_utc = datetime.fromtimestamp(ts_unix, tz=timezone.utc).isoformat()
        db_conn.execute(
            "INSERT INTO strikes "
            "(ts_unix, ts_utc, lat, lon, distance_km, bearing_deg, mds, home_lat, home_lon) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ts_unix, ts_utc, lat, lon, distance_km, bearing_deg, mds_int, HOME_LAT, HOME_LON),
        )
        db_conn.commit()
        stats["logged_session"] += 1
        stats["logged_total"] += 1
    except sqlite3.Error as e:
        console.log(f"[red]Erreur DB : {e}")


# ─── AFFICHAGE RICH ─────────────────────────────────────────────────────────
def make_header():
    elapsed = int(time.time() - stats["started"])
    h, m, s = elapsed // 3600, (elapsed % 3600) // 60, elapsed % 60
    closest = f"{stats['closest']:.1f} km" if stats["closest"] is not None else "—"
    rate = stats["nearby"] / max(elapsed / 60, 1)
    text = (
        f"[bold]Position:[/bold] {HOME_LAT:.4f}°N, {HOME_LON:.4f}°E   "
        f"[bold]Rayon:[/bold] {MAX_DISTANCE_KM} km   "
        f"[bold]Uptime:[/bold] {h:02d}:{m:02d}:{s:02d}\n"
        f"[bold]Total mondial reçu:[/bold] {stats['total']}   "
        f"[bold]Dans la zone:[/bold] {stats['nearby']} "
        f"([dim]{rate:.1f}/min[/dim])   "
        f"[bold]Plus proche:[/bold] {closest}\n"
        f"[bold]DB:[/bold] [green]{DB_PATH}[/green]   "
        f"[bold]Session:[/bold] +{stats['logged_session']}   "
        f"[bold]Total enregistré:[/bold] {stats['logged_total']}"
    )
    return Panel(text, title="⚡ Moniteur foudre — Blitzortung", border_style="blue")

def make_table():
    table = Table(expand=True, show_lines=False)
    table.add_column("Heure UTC", style="cyan", no_wrap=True)
    table.add_column("Distance", justify="right")
    table.add_column("Direction", justify="center")
    table.add_column("Délai son", justify="right", style="magenta")
    table.add_column("Lat, Lon", style="dim")
    table.add_column("Détecteurs", justify="right", style="green")

    for s in reversed(strikes):  # plus récents en haut
        delay_s = (s["distance"] * 1000) / SPEED_OF_SOUND_MS
        delay_str = f"{delay_s:.0f} s" if delay_s < 120 else f"{delay_s / 60:.1f} min"
        dist_color = color_for_distance(s["distance"])
        table.add_row(
            s["time"],
            f"[{dist_color}]{s['distance']:.1f} km[/{dist_color}]",
            f"{cardinal(s['bearing'])} ({s['bearing']:.0f}°)",
            delay_str,
            f"{s['lat']:.3f}, {s['lon']:.3f}",
            str(s["mds"]),
        )
    return table


# ─── CALLBACKS MQTT ─────────────────────────────────────────────────────────
def on_connect(client, userdata, flags, rc):
    if rc == 0:
        console.log(f"[green]✓ Connecté à {BROKER_HOST}, abonnement à {TOPIC}")
        client.subscribe(TOPIC)
    else:
        console.log(f"[red]✗ Échec de connexion (code {rc})")

def on_message(client, userdata, msg):
    try:
        data = json.loads(msg.payload.decode())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return

    lat, lon = data.get("lat"), data.get("lon")
    if lat is None or lon is None:
        return

    stats["total"] += 1
    dist = haversine(HOME_LAT, HOME_LON, lat, lon)
    if dist > MAX_DISTANCE_KM:
        return

    stats["nearby"] += 1
    if stats["closest"] is None or dist < stats["closest"]:
        stats["closest"] = dist

    # time est en nanosecondes Unix
    ts = data.get("time", 0) / 1e9
    brg = bearing(HOME_LAT, HOME_LON, lat, lon)
    mds = len(data.get("sig", [])) or data.get("mds", "?")

    # persistance en base
    log_strike(ts, lat, lon, dist, brg, mds)

    strikes.append({
        "time": datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S"),
        "lat": lat,
        "lon": lon,
        "distance": dist,
        "bearing": brg,
        "mds": mds,    # nombre de stations qui ont détecté l'éclair
    })


# ─── BOUCLE PRINCIPALE ──────────────────────────────────────────────────────
def main():
    # ouverture / création de la base, récupère le total déjà stocké
    existing = init_db()
    stats["logged_total"] = existing
    console.log(f"[green]✓ Base SQLite : {DB_PATH} ({existing} impacts déjà stockés)")

    # paho-mqtt v2 : on précise explicitement l'API callback pour compat large
    try:
        client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION1)
    except AttributeError:
        client = mqtt.Client()  # paho-mqtt v1.x

    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(BROKER_HOST, BROKER_PORT, keepalive=60)
    client.loop_start()

    layout = Layout()
    layout.split_column(
        Layout(name="header", size=6),
        Layout(name="table"),
    )

    try:
        with Live(layout, refresh_per_second=2, console=console, screen=True):
            while True:
                layout["header"].update(make_header())
                layout["table"].update(make_table())
                time.sleep(0.5)
    except KeyboardInterrupt:
        console.log("[yellow]Arrêt demandé, fermeture propre…")
    finally:
        client.loop_stop()
        client.disconnect()
        if db_conn is not None:
            db_conn.close()
            console.log(f"[green]✓ Base fermée. Total enregistré : {stats['logged_total']}")


if __name__ == "__main__":
    main()
