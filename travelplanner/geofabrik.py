"""Geofabrik extract catalog: discover every downloadable region.

Geofabrik publishes its full extract list as JSON. `index-v1-nogeom.json` lists
~555 regions (id, name, parent, and the .osm.pbf URL) without boundary geometry,
which is all that is needed to enumerate regions and resolve a name to a URL.
The index is downloaded once and cached on disk.

    list_regions()            -> every Region (downloads the index if needed)
    catalog()                 -> {id: Region}
    cached_catalog()          -> {id: Region}, or {} if the index isn't cached
                                 (never downloads; safe at runtime/offline)

Coordinate-based selection uses the geometry index (index-v1.json, ~3.8 MB):

    region_for(lat, lon)            -> smallest Region whose polygon contains it
    region_for_trip(o, d)           -> smallest Region containing BOTH endpoints,
                                       or ValueError if none (a cross-border trip
                                       no single extract covers)
"""

import json
import os
import urllib.request
from dataclasses import dataclass
from functools import lru_cache

INDEX_URL = "https://download.geofabrik.de/index-v1-nogeom.json"
_INDEX_FILE = "geofabrik-index-nogeom.json"
GEOM_INDEX_URL = "https://download.geofabrik.de/index-v1.json"
_GEOM_FILE = "geofabrik-index.json"


@dataclass(frozen=True)
class Region:
    id: str
    name: str
    parent: str | None
    pbf_url: str


def _index_path() -> str:
    from travelplanner.roads import cache_dir
    return os.path.join(cache_dir(), _INDEX_FILE)


def _download(url: str, dest: str) -> None:
    tmp = dest + ".part"
    req = urllib.request.Request(url, headers={"User-Agent": "travelplanner"})
    with urllib.request.urlopen(req) as resp, open(tmp, "wb") as out:
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
    os.replace(tmp, dest)


def _parse_catalog(data: dict) -> dict[str, Region]:
    out: dict[str, Region] = {}
    for feature in data.get("features", []):
        props = feature.get("properties", {})
        pbf = (props.get("urls") or {}).get("pbf")
        region_id = props.get("id")
        if not pbf or not region_id:
            continue
        out[region_id] = Region(region_id, props.get("name", region_id),
                                 props.get("parent"), pbf)
    return out


def catalog(*, refresh: bool = False) -> dict[str, Region]:
    """The full catalog, downloading + caching the index if needed."""
    path = _index_path()
    if refresh or not os.path.exists(path):
        _download(INDEX_URL, path)
    with open(path, encoding="utf-8") as f:
        return _parse_catalog(json.load(f))


def cached_catalog() -> dict[str, Region]:
    """The catalog if the index is already cached, else {} (never downloads)."""
    path = _index_path()
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return _parse_catalog(json.load(f))


def list_regions(*, refresh: bool = False) -> list[Region]:
    """Every downloadable region, sorted by id (downloads the index if needed)."""
    return sorted(catalog(refresh=refresh).values(), key=lambda r: r.id)


# --- coordinate -> region selection (geometry index) -----------------------

@dataclass(frozen=True)
class RegionGeometry:
    region: Region
    bbox: tuple[float, float, float, float]   # min_lon, min_lat, max_lon, max_lat
    polygons: tuple                            # MultiPolygon coords (lon, lat)

    @property
    def bbox_area(self) -> float:
        min_lon, min_lat, max_lon, max_lat = self.bbox
        return (max_lon - min_lon) * (max_lat - min_lat)


def _geom_index_path() -> str:
    from travelplanner.roads import cache_dir
    return os.path.join(cache_dir(), _GEOM_FILE)


def _bbox_of(polygons) -> tuple[float, float, float, float]:
    min_lon = min_lat = float("inf")
    max_lon = max_lat = float("-inf")
    for polygon in polygons:
        for ring in polygon:
            for lon, lat in ring:
                min_lon, max_lon = min(min_lon, lon), max(max_lon, lon)
                min_lat, max_lat = min(min_lat, lat), max(max_lat, lat)
    return (min_lon, min_lat, max_lon, max_lat)


def _normalize_polygons(geometry) -> tuple:
    """Coerce a Polygon/MultiPolygon GeoJSON geometry to MultiPolygon coords."""
    if not geometry:
        return ()
    if geometry["type"] == "Polygon":
        return (geometry["coordinates"],)
    return tuple(geometry["coordinates"])


@lru_cache(maxsize=1)
def _load_geom_catalog() -> dict[str, RegionGeometry]:
    path = _geom_index_path()
    if not os.path.exists(path):
        _download(GEOM_INDEX_URL, path)
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    out: dict[str, RegionGeometry] = {}
    for feature in data.get("features", []):
        props = feature.get("properties", {})
        pbf = (props.get("urls") or {}).get("pbf")
        region_id = props.get("id")
        if not pbf or not region_id:
            continue
        polygons = _normalize_polygons(feature.get("geometry"))
        if not polygons:
            continue
        region = Region(region_id, props.get("name", region_id),
                        props.get("parent"), pbf)
        out[region_id] = RegionGeometry(region, _bbox_of(polygons), polygons)
    return out


def _geom_catalog(refresh: bool = False) -> dict[str, RegionGeometry]:
    """The parsed geometry catalog (cached after first build). refresh=True forces
    a fresh download and rebuild on every call -- the cache is invalidated, not
    memoized per-flag (a plain lru_cache on `refresh` would re-download only the
    first refresh=True and then return the stale memoized copy)."""
    if refresh:
        _download(GEOM_INDEX_URL, _geom_index_path())
        _load_geom_catalog.cache_clear()
    return _load_geom_catalog()


def _point_in_ring(lon: float, lat: float, ring) -> bool:
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > lat) != (yj > lat):
            x_cross = (xj - xi) * (lat - yi) / (yj - yi) + xi
            if lon < x_cross:
                inside = not inside
        j = i
    return inside


def _point_in_polygons(lon: float, lat: float, polygons) -> bool:
    for polygon in polygons:
        if not polygon:
            continue
        if _point_in_ring(lon, lat, polygon[0]) and not any(
                _point_in_ring(lon, lat, hole) for hole in polygon[1:]):
            return True
    return False


def _smallest_containing(geom_catalog: dict, coords) -> Region | None:
    """Smallest non-continent region whose polygon contains every coordinate.

    Parent-None extracts (continents, plus country-scale aggregates like `russia`
    and `central-america`) are skipped on purpose: they are multi-GB and must
    never be auto-downloaded silently. A point covered only by such an extract
    (e.g. a Russian Arctic island outside every federal-district sub-extract)
    returns None here, and the caller raises an error telling the user to pass an
    explicit region -- the same stance as the cross-border case.
    """
    best = None
    for rg in geom_catalog.values():
        if rg.region.parent is None:
            continue  # skip continents/planet (too large to auto-select)
        min_lon, min_lat, max_lon, max_lat = rg.bbox
        if any(not (min_lon <= lon <= max_lon and min_lat <= lat <= max_lat)
               for lat, lon in coords):
            continue
        if all(_point_in_polygons(lon, lat, rg.polygons) for lat, lon in coords):
            if best is None or rg.bbox_area < best.bbox_area:
                best = rg
    return best.region if best is not None else None


def region_for(lat: float, lon: float, *, refresh: bool = False) -> Region:
    """Smallest region whose polygon contains the point (raises if none)."""
    region = _smallest_containing(_geom_catalog(refresh), [(lat, lon)])
    if region is None:
        raise ValueError(
            f"No Geofabrik region covers ({lat}, {lon}). Pass an explicit region "
            f"or a local .osm.pbf path.")
    return region


def region_for_trip(origin, dest, *, refresh: bool = False) -> Region:
    """Smallest single region containing both endpoints (origin/dest are
    (lat, lon)). Raises if none does -- a cross-border trip no single extract
    covers (use plan() for multimodal, or pass a region explicitly)."""
    return region_for_points([origin, dest], refresh=refresh)


def region_for_points(points, *, refresh: bool = False) -> Region:
    """Smallest single region containing all (lat, lon) points (raises if none)."""
    region = _smallest_containing(_geom_catalog(refresh), list(points))
    if region is None:
        raise ValueError(
            "No single Geofabrik region covers all of these points. They likely "
            "span a border or water (e.g. Amsterdam->London): no one road extract "
            "connects them. Use plan() for a multimodal trip, or pass a region "
            "(e.g. a merged/continent extract) explicitly.")
    return region
