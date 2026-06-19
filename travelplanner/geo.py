"""Geographic distance helpers."""

from math import asin, cos, radians, sin, sqrt

EARTH_RADIUS_KM = 6371.0088


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km between two lat/lon points."""
    rlat1, rlat2 = radians(lat1), radians(lat2)
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(rlat1) * cos(rlat2) * sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * asin(sqrt(a))


def road_distance(lat1: float, lon1: float, lat2: float, lon2: float,
                  detour_factor: float = 1.3) -> float:
    """Estimated road distance: great-circle scaled by a detour factor."""
    return haversine(lat1, lon1, lat2, lon2) * detour_factor
