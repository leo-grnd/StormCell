"""Géocodage inverse (commune la plus proche) via Nominatim, débité et tolérant.

Nominatim impose ≤ 1 requête/s et un User-Agent identifiable. On respecte cela
avec un verrou + une attente ; le cache (table `geocache`) évite les appels
répétés. Toute erreur réseau renvoie simplement `None` (fonctionnalité optionnelle).
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
_USER_AGENT = "StormCell/0.2 (Blitzortung lightning monitor; personal use)"
_MIN_INTERVAL_S = 1.1

_lock = asyncio.Lock()
_last_call = 0.0
# Ordre de préférence des champs d'adresse pour un nom court et lisible.
_ADDRESS_KEYS = ("village", "town", "city", "municipality", "hamlet", "county", "state")


async def reverse_nominatim(lat: float, lon: float, lang: str = "fr") -> str | None:
    """Renvoie le nom de la localité la plus proche, ou None si indisponible."""
    global _last_call
    try:
        import httpx
    except ImportError:
        logger.warning("httpx absent : géocodage désactivé")
        return None

    async with _lock:
        loop = asyncio.get_event_loop()
        wait = _MIN_INTERVAL_S - (loop.time() - _last_call)
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            async with httpx.AsyncClient(timeout=5.0, headers={"User-Agent": _USER_AGENT}) as client:
                resp = await client.get(
                    NOMINATIM_URL,
                    params={
                        "format": "jsonv2", "lat": lat, "lon": lon,
                        "zoom": 10, "accept-language": lang,
                    },
                )
            _last_call = loop.time()
        except Exception:
            logger.debug("Échec requête Nominatim", exc_info=True)
            return None

    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    address = data.get("address", {})
    for key in _ADDRESS_KEYS:
        if address.get(key):
            return str(address[key])
    return data.get("name") or None
