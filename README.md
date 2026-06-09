# ⚡ StormCell — Moniteur d'orages Blitzortung

Suivi temps réel des impacts de foudre et **nowcasting de cellules orageuses**.
StormCell se connecte au broker MQTT communautaire [Blitzortung](https://www.blitzortung.org/),
filtre les impacts autour d'une position (HOME), les stocke en SQLite, détecte et
**suit les cellules orageuses** (DBSCAN + filtre de Kalman), puis prédit leur
trajectoire, leur heure d'arrivée et leur potentiel de sévérité — le tout dans un
dashboard web temps réel (carte Leaflet + WebSocket).

> Usage privé / divertissement, conformément à la politique d'utilisation de Blitzortung.org.

## Fonctionnalités

- **Carte live** des impacts (rendu canvas, tient des milliers de points) + historique rejouable.
- **Détection de cellules** par DBSCAN en métrique haversine (valide partout sur le globe).
- **Tracking par filtre de Kalman** : vitesse/cap lissés + cône d'incertitude principiel.
- **Prédiction** : ETA du centroïde **et** ETA au bord (foudre dans l'anneau d'alerte),
  proba de coup sur HOME, tendances intensité/rayon.
- **Lightning jump** (détecteur 2σ) : flambée du taux d'éclairs → signal d'orage sévère.
- **Indice de sévérité 0–5** et **lignée split/merge** des cellules.
- **Alerte** sonore + visuelle quand un impact entre dans l'anneau configuré.
- Persistance SQLite (écritures batchées) + commande `stats` (rapport texte).

## Installation

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

(Sur Linux/macOS : `python -m venv .venv && ./.venv/bin/pip install -e ".[dev]"`)

## Lancement

```powershell
blitz web              # dashboard web (défaut) → http://127.0.0.1:8000
blitz tui              # moniteur terminal (Rich)
blitz stats --days 30  # rapport texte depuis la base
```

Équivalent sans la commande installée : `python -m blitz web`.
Options : `blitz --config autre.toml --log-level DEBUG web --port 8123`.

## Configuration

Tout est optionnel — les clés absentes prennent les défauts de `src/blitz/config.py`.
Voir [`config.toml`](config.toml) pour l'exemple commenté complet.

| Section | Clés notables |
|---|---|
| `[home]` | `lat`, `lon` — position de référence |
| `[filter]` | `max_distance_km`, `alert_distance_km` |
| `[mqtt]` | `host`, `port`, `topic`, reconnexion |
| `[db]` | `path` |
| `[web]` | `host`, `port` |
| `[analysis]` | `cluster_eps_km`, `cluster_min_samples`, `cell_window_minutes`, `tick_seconds`, `recent_buffer`, `kf_meas_noise_km`, `kf_process_accel`, `jump_window_min`, `strike_ring_km`, `nowcast_horizon_min` |
| `[log]` | `file`, `max_bytes`, `backups` |

## Architecture

```
src/blitz/
  mqtt_worker.py   # client MQTT (thread) → filtre par distance → queue + listener
  state.py         # état partagé thread-safe (Strike, Cell, SharedState)
  db.py            # SQLite : écritures batchées + verrou, lectures filtrées
  analysis.py      # DBSCAN → association → Kalman → ETA/proba/jump/sévérité
  tracking.py      # filtre de Kalman, lightning jump, sévérité, nowcast
  geo.py           # haversine, bearing, projections locales
  server.py        # FastAPI : REST + WebSocket + boucles MQTT/analyse
  cli.py           # entrée `blitz web|tui|stats`
  web/             # frontend Leaflet (app.js, index.html, style.css)
```

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest
```

## Licence

MIT — voir [LICENSE](LICENSE).
