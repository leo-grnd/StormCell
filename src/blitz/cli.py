"""Point d'entrée CLI : `python -m blitz web|tui|stats`."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Config, load_config
from .logging_setup import configure as configure_logging

logger = logging.getLogger("blitz.cli")


def cmd_web(cfg: Config) -> int:
    import uvicorn

    from .server import create_app

    app = create_app(cfg)
    logger.info("Démarrage du serveur web sur http://%s:%s", cfg.web.host, cfg.web.port)
    uvicorn.run(app, host=cfg.web.host, port=cfg.web.port, log_level="warning")
    return 0


def cmd_tui(cfg: Config) -> int:
    """Lance l'ancien moniteur TUI Rich (équivalent au script d'origine)."""
    from rich.console import Console
    from rich.layout import Layout
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table

    from .db import Database
    from .geo import SPEED_OF_SOUND_MS, cardinal, color_for_distance
    from .mqtt_worker import MqttWorker
    from .state import SharedState, Strike

    console = Console()
    state = SharedState(max_strikes_recent=cfg.analysis.recent_buffer)
    db = Database(Path(cfg.db.path))
    state.stats["logged_total"] = db.count()
    console.log(f"[green]✓ Base SQLite : {cfg.db.path} ({state.stats['logged_total']} impacts)")

    def on_nearby(s: Strike) -> None:
        db.insert_strike(
            ts_unix=s.ts_unix, lat=s.lat, lon=s.lon,
            distance_km=s.distance_km, bearing_deg=s.bearing_deg, mds=s.mds,
            home_lat=cfg.home.lat, home_lon=cfg.home.lon,
        )
        with state.lock:
            state.stats["logged_session"] += 1
            state.stats["logged_total"] += 1

    worker = MqttWorker(cfg, state, on_nearby=on_nearby)
    worker.start()

    def make_header() -> Panel:
        snap = state.snapshot_stats()
        elapsed = int(time.time() - snap["started_at"])
        h, m, s = elapsed // 3600, (elapsed % 3600) // 60, elapsed % 60
        closest = f"{snap['closest_km']:.1f} km" if snap["closest_km"] is not None else "—"
        rate = snap["nearby"] / max(elapsed / 60, 1)
        text = (
            f"[bold]Position:[/bold] {cfg.home.lat:.4f}°N, {cfg.home.lon:.4f}°E   "
            f"[bold]Rayon:[/bold] {cfg.filter.max_distance_km} km   "
            f"[bold]Uptime:[/bold] {h:02d}:{m:02d}:{s:02d}\n"
            f"[bold]Total mondial reçu:[/bold] {snap['total_world']}   "
            f"[bold]Dans la zone:[/bold] {snap['nearby']} "
            f"([dim]{rate:.1f}/min[/dim])   "
            f"[bold]Plus proche:[/bold] {closest}\n"
            f"[bold]DB:[/bold] [green]{cfg.db.path}[/green]   "
            f"[bold]Session:[/bold] +{snap['logged_session']}   "
            f"[bold]Total enregistré:[/bold] {snap['logged_total']}"
        )
        return Panel(text, title="⚡ Moniteur foudre — Blitzortung", border_style="blue")

    def make_table() -> Table:
        table = Table(expand=True, show_lines=False)
        table.add_column("Heure UTC", style="cyan", no_wrap=True)
        table.add_column("Distance", justify="right")
        table.add_column("Direction", justify="center")
        table.add_column("Délai son", justify="right", style="magenta")
        table.add_column("Lat, Lon", style="dim")
        table.add_column("Détecteurs", justify="right", style="green")
        with state.lock:
            items = list(state.recent)[-cfg.filter.max_strikes_shown:]
        for s in reversed(items):
            delay_s = (s.distance_km * 1000) / SPEED_OF_SOUND_MS
            delay_str = f"{delay_s:.0f} s" if delay_s < 120 else f"{delay_s / 60:.1f} min"
            dist_color = color_for_distance(s.distance_km)
            table.add_row(
                datetime.fromtimestamp(s.ts_unix, tz=timezone.utc).strftime("%H:%M:%S"),
                f"[{dist_color}]{s.distance_km:.1f} km[/{dist_color}]",
                f"{cardinal(s.bearing_deg)} ({s.bearing_deg:.0f}°)",
                delay_str,
                f"{s.lat:.3f}, {s.lon:.3f}",
                str(s.mds if s.mds is not None else "?"),
            )
        return table

    layout = Layout()
    layout.split_column(Layout(name="header", size=6), Layout(name="table"))

    try:
        with Live(layout, refresh_per_second=2, console=console, screen=True):
            while True:
                layout["header"].update(make_header())
                layout["table"].update(make_table())
                time.sleep(0.5)
    except KeyboardInterrupt:
        console.log("[yellow]Arrêt demandé…")
    finally:
        worker.stop()
        db.close()
        console.log(f"[green]✓ Total enregistré : {state.snapshot_stats()['logged_total']}")
    return 0


def cmd_stats(cfg: Config, days: int) -> int:
    """Affiche un rapport texte de l'historique SQLite sur N jours."""
    from .db import Database
    from .geo import cardinal

    db = Database(Path(cfg.db.path))
    cutoff = (datetime.now(tz=timezone.utc) - timedelta(days=days)).timestamp()
    rows = db.query_range(from_unix=cutoff, limit=10_000_000)
    total_all = db.count()
    db.close()

    print(f"Base SQLite : {cfg.db.path}")
    print(f"Total tous temps : {total_all}")
    print(f"Fenêtre analysée : {days} dernier(s) jour(s) ({len(rows)} impacts)")
    if not rows:
        return 0

    bearings = Counter(cardinal(r["bearing_deg"]) for r in rows)
    print("\nRépartition par direction :")
    for direction, n in bearings.most_common():
        bar = "█" * int(40 * n / len(rows))
        print(f"  {direction:>3}  {n:5d}  {bar}")

    dists = [r["distance_km"] for r in rows]
    print(f"\nDistance min : {min(dists):.1f} km")
    print(f"Distance moyenne : {sum(dists)/len(dists):.1f} km")
    print(f"Distance médiane : {sorted(dists)[len(dists)//2]:.1f} km")
    return 0


def cmd_bench(args: argparse.Namespace) -> int:
    """Banc d'essai du pipeline de clustering/tracking (orage synthétique)."""
    from .bench import run_bench

    return run_bench(
        strikes=args.strikes, cells=args.cells, ticks=args.ticks,
        radius_km=args.radius, seed=args.seed, compare=args.compare,
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m blitz", description="Moniteur Blitzortung")
    p.add_argument("--config", type=Path, help="Fichier de config TOML")
    p.add_argument("--log-level", default="INFO", help="DEBUG|INFO|WARNING|ERROR")
    sub = p.add_subparsers(dest="cmd")

    sp_web = sub.add_parser("web", help="Dashboard web (défaut)")
    sp_web.add_argument("--host", default=None)
    sp_web.add_argument("--port", type=int, default=None)

    sub.add_parser("tui", help="Moniteur TUI Rich (rétro-compat)")

    sp_stats = sub.add_parser("stats", help="Rapport texte depuis la DB")
    sp_stats.add_argument("--days", type=int, default=30)

    sp_bench = sub.add_parser("bench", help="Banc d'essai perf du pipeline d'analyse")
    sp_bench.add_argument("--strikes", type=int, default=15_000, help="strokes par tick")
    sp_bench.add_argument("--cells", type=int, default=8, help="nombre de cellules simulées")
    sp_bench.add_argument("--ticks", type=int, default=12, help="nombre de ticks rejoués")
    sp_bench.add_argument("--radius", type=float, default=12.0, help="rayon des cellules (km)")
    sp_bench.add_argument("--seed", type=int, default=1234)
    sp_bench.add_argument("--compare", action="store_true", help="comparer grille ON vs OFF")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)
    configure_logging(level=args.log_level, log_cfg=cfg.log)
    if cfg.source_path is not None:
        logger.info("Config chargée : %s", cfg.source_path)
    else:
        logger.info("Aucun config.toml trouvé — valeurs par défaut")

    cmd = args.cmd or "web"
    if cmd == "web":
        if getattr(args, "host", None):
            cfg.web.host = args.host
        if getattr(args, "port", None):
            cfg.web.port = args.port
        return cmd_web(cfg)
    if cmd == "tui":
        return cmd_tui(cfg)
    if cmd == "stats":
        return cmd_stats(cfg, args.days)
    if cmd == "bench":
        return cmd_bench(args)
    print(f"Commande inconnue : {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
