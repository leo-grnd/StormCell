"""Vérification des prédictions : compare ce qui a été annoncé à ce qui s'est passé.

On confronte les **prédictions** émises (une cellule menace d'amener la foudre dans
l'anneau, avec un ETA) aux **arrivées réelles** (impacts effectivement entrés dans
l'anneau). On en tire une table de contingence et les scores classiques du nowcasting :

- POD (Probability Of Detection) = arrivées détectées / arrivées totales
- FAR (False Alarm Ratio)        = fausses alertes / prédictions totales
- CSI (Critical Success Index)   = détections / (arrivées + fausses alertes)
- erreur d'ETA moyenne et préavis (lead time) moyen sur les coups réussis
"""

from __future__ import annotations

from typing import Any


def arrival_events(in_ring_ts: list[float], gap_min: float) -> list[float]:
    """Regroupe les impacts entrés dans l'anneau en *événements d'arrivée*.

    Deux impacts séparés de plus de `gap_min` minutes appartiennent à deux
    événements distincts. Renvoie l'instant d'**onset** de chaque événement.
    """
    gap = gap_min * 60.0
    events: list[float] = []
    last: float | None = None
    for ts in sorted(in_ring_ts):
        if last is None or ts - last > gap:
            events.append(ts)
        last = ts
    return events


def evaluate(
    predictions: list[dict[str, Any]],
    in_ring_ts: list[float],
    tolerance_min: float = 10.0,
    gap_min: float = 20.0,
) -> dict[str, Any]:
    """Construit la table de contingence et les scores de skill.

    Une prédiction est un **hit** si son `predicted_arrival` tombe à ±`tolerance_min`
    d'un onset d'arrivée réel, sinon une **fausse alerte**. Un événement d'arrivée
    non couvert par au moins une prédiction est un **raté** (miss).
    """
    events = arrival_events(in_ring_ts, gap_min)
    tol = tolerance_min * 60.0

    detected_events: set[int] = set()
    hits = 0
    pred_results: list[dict[str, Any]] = []

    for p in predictions:
        pa = p.get("predicted_arrival")
        match_idx: int | None = None
        if pa is not None:
            best = tol
            for i, ev in enumerate(events):
                d = abs(ev - pa)
                if d <= best:
                    best, match_idx = d, i
        if match_idx is not None:
            hits += 1
            detected_events.add(match_idx)
            actual = events[match_idx]
            pred_results.append({
                **p,
                "outcome": "hit",
                "actual_arrival": actual,
                "eta_error_min": (actual - pa) / 60.0,
                "lead_min": (pa - p["ts_made"]) / 60.0,
            })
        else:
            pred_results.append({
                **p, "outcome": "false_alarm",
                "actual_arrival": None, "eta_error_min": None,
                "lead_min": ((pa - p["ts_made"]) / 60.0) if pa is not None else None,
            })

    total_events = len(events)
    total_preds = len(predictions)
    events_detected = len(detected_events)
    false_alarms = total_preds - hits
    misses = total_events - events_detected

    def _ratio(num: int, den: int) -> float | None:
        return round(num / den, 3) if den > 0 else None

    hit_preds = [r for r in pred_results if r["outcome"] == "hit"]
    eta_errors = [abs(r["eta_error_min"]) for r in hit_preds if r["eta_error_min"] is not None]
    leads = [r["lead_min"] for r in hit_preds if r["lead_min"] is not None]

    return {
        "total_predictions": total_preds,
        "total_arrivals": total_events,
        "events_detected": events_detected,
        "hits": hits,
        "false_alarms": false_alarms,
        "misses": misses,
        "pod": _ratio(events_detected, total_events),
        "far": _ratio(false_alarms, total_preds),
        "csi": _ratio(events_detected, total_events + false_alarms),
        "mean_eta_error_min": round(sum(eta_errors) / len(eta_errors), 1) if eta_errors else None,
        "mean_lead_min": round(sum(leads) / len(leads), 1) if leads else None,
        # les 50 prédictions les plus récentes, déjà résolues, pour l'affichage
        "recent": sorted(pred_results, key=lambda r: r["ts_made"], reverse=True)[:50],
    }
