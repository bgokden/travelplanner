"""Street-accurate driving via on-demand OpenStreetMap extracts.

The first time you route in a region, its OpenStreetMap extract is downloaded
from Geofabrik and cached on disk; a Customizable Contraction Hierarchies index
is then built (in memory, per process). After the first download everything is
offline and fast.

    drive(origin, dest, region="switzerland")
      -> DriveResult(drivable=True, duration=..., distance_km=...)
      -> DriveResult(drivable=False)   when no road connects them

Requires the `road` extra (routingkit-cch + osmium) and internet for the first
download of each region. `region` may be a known name, a Geofabrik URL, or a
local .osm.pbf path.
"""

import os
import urllib.request
from dataclasses import dataclass
from datetime import date, timedelta
from functools import lru_cache

from travelplanner.catalog import resolve_city
from travelplanner.geo import haversine
from travelplanner.models import Location, LocationType

_GEOFABRIK = "https://download.geofabrik.de"
REGIONS = {
    "liechtenstein": f"{_GEOFABRIK}/europe/liechtenstein-latest.osm.pbf",
    "switzerland": f"{_GEOFABRIK}/europe/switzerland-latest.osm.pbf",
    "austria": f"{_GEOFABRIK}/europe/austria-latest.osm.pbf",
    "germany": f"{_GEOFABRIK}/europe/germany-latest.osm.pbf",
    "france": f"{_GEOFABRIK}/europe/france-latest.osm.pbf",
    "italy": f"{_GEOFABRIK}/europe/italy-latest.osm.pbf",
    "great-britain": f"{_GEOFABRIK}/europe/great-britain-latest.osm.pbf",
    "netherlands": f"{_GEOFABRIK}/europe/netherlands-latest.osm.pbf",
    "spain": f"{_GEOFABRIK}/europe/spain-latest.osm.pbf",
}


@dataclass(frozen=True)
class DriveResult:
    drivable: bool
    duration: timedelta | None = None
    distance_km: float | None = None


def cache_dir() -> str:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    path = os.path.join(base, "travelplanner")
    os.makedirs(path, exist_ok=True)
    return path


def resolve_region(region: str) -> str:
    if region.endswith(".osm.pbf"):
        return region  # a URL or local path
    if region.startswith("http://") or region.startswith("https://"):
        return region
    key = region.strip().lower()
    if key in REGIONS:
        return REGIONS[key]
    raise ValueError(
        f"Unknown region {region!r}. Use a known name "
        f"({', '.join(sorted(REGIONS))}), a Geofabrik URL, or a local .osm.pbf path.")


def download_region(region: str) -> str:
    """Return a local path to the region's .osm.pbf, downloading + caching it."""
    src = resolve_region(region)
    if not src.startswith("http"):
        if not os.path.exists(src):
            raise FileNotFoundError(src)
        return src
    dest = os.path.join(cache_dir(), src.rsplit("/", 1)[-1])
    if not os.path.exists(dest):
        tmp = dest + ".part"
        req = urllib.request.Request(src, headers={"User-Agent": "travelplanner"})
        with urllib.request.urlopen(req) as resp, open(tmp, "wb") as out:
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
        os.replace(tmp, dest)
    return dest


def prefetch(regions, *, build: bool = False) -> list[str]:
    """Download (and optionally build) regions ahead of time.

    Call this in a build/CI step (e.g. a Dockerfile) so the slow network
    download happens then, not on the first runtime request. Returns the cached
    .osm.pbf paths. The cache location honours XDG_CACHE_HOME (default
    ~/.cache/travelplanner), so point it at your image layer to bake data in.

    build=True also constructs the CCH index to surface build errors early; note
    the in-memory index does not persist across processes, but the downloaded
    extract (the slow part) is cached on disk.
    """
    if isinstance(regions, str):
        regions = [regions]
    paths = []
    for region in regions:
        paths.append(download_region(region))
        if build:
            road_router(region)
    return paths


@lru_cache(maxsize=4)
def road_router(region: str):
    """A CCHRoadRouter for the region (downloaded + built once, then cached)."""
    from travelplanner.graph.road.osm import load_road_graph
    from travelplanner.graph.road import CCHRoadRouter

    graph = load_road_graph(download_region(region), store_names=False)
    return CCHRoadRouter(graph)


def region_connector(region: str, stops, **kwargs):
    """A street-accurate CCHConnector backed by the region's road network."""
    from travelplanner.graph.coupling import CCHConnector

    return CCHConnector(road_router(region), stops, **kwargs)


def _coerce(point) -> Location:
    if isinstance(point, Location):
        return point
    if isinstance(point, (tuple, list)) and len(point) == 2:
        return Location(f"{point[0]},{point[1]}", LocationType.LANDMARK,
                        float(point[0]), float(point[1]))
    text = str(point).strip()
    if "," in text:
        a, b = text.split(",", 1)
        try:
            return Location(text, LocationType.LANDMARK, float(a), float(b))
        except ValueError:
            pass
    lat, lon = resolve_city(text)
    return Location(text, LocationType.CITY, lat, lon)


# A point further than this from any road node is not covered by the region's
# data; snapping it anyway would give a misleading answer.
MAX_SNAP_KM = 25.0


def _nearest_node(graph, lat: float, lon: float) -> tuple[int, float]:
    best_i, best_d = 0, float("inf")
    for i in range(graph.node_count):
        d = haversine(lat, lon, graph.latitude[i], graph.longitude[i])
        if d < best_d:
            best_i, best_d = i, d
    return best_i, best_d


def _snap(graph, point: Location, region: str) -> str:
    idx, dist = _nearest_node(graph, point.lat, point.lon)
    if dist > MAX_SNAP_KM:
        raise ValueError(
            f"{point.name!r} ({point.lat},{point.lon}) is not within the "
            f"{region!r} road data (nearest road is {dist:.0f} km away). "
            f"Use a region that covers it.")
    return graph.key(idx)


def _path_distance_km(graph, node_keys) -> float:
    total = 0.0
    idx = [graph.index(k) for k in node_keys]
    for a, b in zip(idx, idx[1:]):
        total += haversine(graph.latitude[a], graph.longitude[a],
                           graph.latitude[b], graph.longitude[b])
    return total


def drive(origin, dest, region: str, *, day: date | None = None,
          conditions: frozenset = frozenset()) -> DriveResult:
    """Street-accurate driving estimate, or drivable=False if no road connects."""
    o = _coerce(origin)
    d = _coerce(dest)
    router = road_router(region)
    g = router.graph
    road = router.customize(day or date.today(), conditions)
    path = road.route(_snap(g, o, region), _snap(g, d, region))
    if path is None:
        return DriveResult(drivable=False)
    return DriveResult(drivable=True,
                       duration=timedelta(seconds=path.seconds),
                       distance_km=round(_path_distance_km(g, path.node_keys), 1))
