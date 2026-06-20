"""Pluggable geocoding: resolve a place name to (lat, lon).

A geocoder is just a callable ``(name) -> (lat, lon) | None`` -- it returns
coordinates, or None meaning "I can't resolve this, try the next one". Small
pieces compose into whatever policy you need:

    bundled_geocoder            the bundled city table (offline, the default)
    chain(g1, g2, ...)          try each in order; first non-None wins
    cached(geocoder)            wrap with a JSON disk cache of hits
    nominatim_geocoder(...)     online OpenStreetMap lookup (opt-in, network)

The active geocoder (used by city(), drive(), and "name" inputs) defaults to the
bundled table, so nothing reaches the network unless you opt in:

    set_geocoder(chain(bundled_geocoder,
                       cached(nominatim_geocoder(user_agent="myapp"))))

Pre-warm the cache at build time and the same names resolve offline at runtime
(network failures degrade to None, never an exception) -- mirroring the offline
road-artifact model.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Optional

from travelplanner.catalog import lookup_city

Geocoder = Callable[[str], Optional[tuple[float, float]]]

_GEOCODE_CACHE_FILE = "geocode-cache.json"


def bundled_geocoder(name: str) -> Optional[tuple[float, float]]:
    """Resolve from the bundled city table (offline). None if not present."""
    return lookup_city(name)


def chain(*geocoders: Geocoder) -> Geocoder:
    """Try each geocoder in order; return the first non-None result."""
    def resolve(name: str) -> Optional[tuple[float, float]]:
        for geocoder in geocoders:
            coords = geocoder(name)
            if coords is not None:
                return coords
        return None
    return resolve


def _default_cache_path() -> str:
    from travelplanner.roads import cache_dir
    return os.path.join(cache_dir(), _GEOCODE_CACHE_FILE)


def cached(geocoder: Geocoder, *, path: Optional[str] = None) -> Geocoder:
    """Wrap a geocoder with a JSON disk cache of successful lookups.

    Hits are persisted (keyed by normalized name), so they resolve offline on a
    later run; misses are not cached. Defaults to a file in the shared cache dir.
    """
    cache_path = path or _default_cache_path()
    store: Optional[dict] = None

    def _load() -> dict:
        nonlocal store
        if store is None:
            if os.path.exists(cache_path):
                with open(cache_path, encoding="utf-8") as f:
                    store = {k: tuple(v) for k, v in json.load(f).items()}
            else:
                store = {}
        return store

    def resolve(name: str) -> Optional[tuple[float, float]]:
        key = name.strip().lower()
        cache = _load()
        if key in cache:
            return cache[key]
        coords = geocoder(name)
        if coords is not None:
            cache[key] = coords
            os.makedirs(os.path.dirname(cache_path) or ".", exist_ok=True)
            tmp = cache_path + ".part"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({k: list(v) for k, v in cache.items()}, f)
            os.replace(tmp, cache_path)
        return coords

    return resolve


NOMINATIM_URL = "https://nominatim.openstreetmap.org"


def _nominatim_get(path: str, params: dict, *, user_agent: str,
                   base_url: str, timeout: float):
    """GET a Nominatim endpoint and return parsed JSON, or None on any error."""
    url = f"{base_url}/{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None


def nominatim_geocoder(*, user_agent: str, base_url: str = NOMINATIM_URL,
                       timeout: float = 10.0) -> Geocoder:
    """Online OpenStreetMap (Nominatim) geocoder. Opt-in; needs the network.

    `user_agent` is required by the Nominatim usage policy (identify your app).
    Returns None on no match or any network error, so it slots into a chain and
    stays offline-safe. Be mindful of the public endpoint's rate limits; wrap in
    cached(...) and pre-warm at build time for production use.
    """
    def resolve(name: str) -> Optional[tuple[float, float]]:
        data = _nominatim_get("search", {"q": name, "format": "json", "limit": 1},
                              user_agent=user_agent, base_url=base_url,
                              timeout=timeout)
        if not data:
            return None
        try:
            return float(data[0]["lat"]), float(data[0]["lon"])
        except (KeyError, IndexError, TypeError, ValueError):
            return None
    return resolve


def nominatim_search(query: str, *, user_agent: str, base_url: str = NOMINATIM_URL,
                     timeout: float = 10.0, limit: int = 8) -> list[dict]:
    """Online OSM search: up to `limit` candidate places for autocomplete.

    Each candidate is {"name": display_name, "lat": float, "lon": float}. Returns
    [] on no match or any network error (offline-safe). For typeahead, debounce
    and cache calls to respect the Nominatim usage policy (~1 req/s; identify the
    app via `user_agent`).
    """
    q = query.strip()
    if not q:
        return []
    data = _nominatim_get("search", {"q": q, "format": "json", "limit": limit},
                          user_agent=user_agent, base_url=base_url, timeout=timeout)
    if not isinstance(data, list):
        return []
    results = []
    for item in data:
        try:
            lat, lon = float(item["lat"]), float(item["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        results.append({"name": item.get("display_name") or f"{lat:.4f}, {lon:.4f}",
                        "lat": lat, "lon": lon})
    return results


_active: Geocoder = bundled_geocoder


def set_geocoder(geocoder: Geocoder) -> None:
    """Set the active geocoder used by city(), drive(), and name inputs."""
    global _active
    _active = geocoder


def get_geocoder() -> Geocoder:
    """The active geocoder."""
    return _active


def reset_geocoder() -> None:
    """Restore the default (bundled, offline) geocoder."""
    global _active
    _active = bundled_geocoder


def resolve_city(name: str, *, geocoder: Optional[Geocoder] = None) -> tuple[float, float]:
    """Resolve a place name to (lat, lon); raise if it cannot be resolved.

    Uses `geocoder` if given, else the active geocoder (default: bundled table).
    """
    coords = (geocoder or _active)(name)
    if coords is None:
        raise ValueError(
            f"Could not resolve location {name!r}. Provide lat/lon explicitly, "
            f"use a bundled city, or register a geocoder with set_geocoder() "
            f"(e.g. an online nominatim_geocoder).")
    return coords


def geocode(name: str) -> tuple[float, float]:
    """Resolve a place name to (lat, lon) via the active geocoder (raises)."""
    return resolve_city(name)
