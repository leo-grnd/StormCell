"""Géométrie et conversions sphériques."""

from __future__ import annotations

import math

EARTH_RADIUS_KM: float = 6371.0
SPEED_OF_SOUND_MS: float = 340.0  # m/s à ~15 °C

_CARDINALS = (
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSO", "SO", "OSO", "O", "ONO", "NO", "NNO",
)


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance en kilomètres entre deux points (formule de haversine)."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Azimut en degrés depuis le point 1 vers le point 2 (0 = Nord, sens horaire)."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlmb = math.radians(lon2 - lon1)
    y = math.sin(dlmb) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlmb)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def cardinal(deg: float) -> str:
    """Convertit un azimut en point cardinal (16 directions)."""
    return _CARDINALS[int((deg + 11.25) // 22.5) % 16]


def color_for_distance(d_km: float) -> str:
    """Code couleur Rich/CSS-like selon la proximité."""
    if d_km < 15:
        return "bold red"
    if d_km < 30:
        return "orange3"
    if d_km < 250:
        return "yellow"
    return "green"


def project_local(home_lat: float, home_lon: float, lat: float, lon: float) -> tuple[float, float]:
    """Projection azimutale équidistante locale (km est, km nord) depuis HOME.

    Précision suffisante pour des distances < 300 km. Utilisée par le clustering
    DBSCAN qui réclame une métrique cartésienne pour un tuning d'`eps` simple.
    """
    phi_h = math.radians(home_lat)
    dlat = math.radians(lat - home_lat)
    dlon = math.radians(lon - home_lon)
    x = EARTH_RADIUS_KM * dlon * math.cos(phi_h)
    y = EARTH_RADIUS_KM * dlat
    return x, y


def unproject_local(home_lat: float, home_lon: float, x_km: float, y_km: float) -> tuple[float, float]:
    """Inverse de project_local : (km est, km nord) → (lat, lon)."""
    phi_h = math.radians(home_lat)
    lat = home_lat + math.degrees(y_km / EARTH_RADIUS_KM)
    lon = home_lon + math.degrees(x_km / (EARTH_RADIUS_KM * math.cos(phi_h)))
    return lat, lon
