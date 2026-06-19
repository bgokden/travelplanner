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

For offline deployment, prepare a region at build time and load it with no
network at runtime:

    build_region("switzerland", out_dir)          # build step (needs internet)
    drive(origin, dest, "switzerland", data_dir=out_dir)   # runtime (offline)

build_region writes the parsed road graph and the CCH contraction order (the
slow, machine-independent steps) to out_dir; data_dir loads them back, skipping
the download, the OSM parse, and the order computation.
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
    """Outcome of a driving query.

    drivable: whether a road route connects origin and destination.
    duration: travel time along the routed road path.
    distance_km: length of that routed path (the sum of straight segments
        between consecutive OpenStreetMap nodes along it), in km. This is the
        driven road distance, not straight-line origin->destination distance.

    Results are direction-dependent: drive(a, b) may differ from drive(b, a)
    (one-way streets, turn restrictions) -- a driving matrix is not symmetric.
    """

    drivable: bool
    duration: timedelta | None = None
    distance_km: float | None = None

    def to_dict(self) -> dict:
        """JSON-safe dict (duration -> seconds)."""
        return {
            "drivable": self.drivable,
            "duration_s": (self.duration.total_seconds()
                           if self.duration is not None else None),
            "distance_km": self.distance_km,
        }


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
def _road_router_cached(region: str, data_dir: str | None):
    from travelplanner.graph.road import CCHRoadRouter

    if data_dir is not None:
        from travelplanner.graph.road.store import load_road_artifact
        graph, order = load_road_artifact(data_dir)
        return CCHRoadRouter(graph, order=order)

    from travelplanner.graph.road.osm import load_road_graph
    graph = load_road_graph(download_region(region), store_names=False)
    return CCHRoadRouter(graph)


def road_router(region: str, data_dir: str | None = None):
    """A CCHRoadRouter for the region (built once per process, then cached).

    With data_dir, load a prebuilt offline artifact (no network, no OSM parse,
    no contraction-order computation). Otherwise download the OSM extract and
    build from scratch.

    The cache key is normalized so road_router(region) and
    road_router(region, None) resolve to the same cached instance.
    """
    return _road_router_cached(region, data_dir)


# Expose the underlying cache controls on the public function.
road_router.cache_clear = _road_router_cached.cache_clear
road_router.cache_info = _road_router_cached.cache_info


def build_region(region: str, out_dir: str) -> str:
    """Build an offline road artifact for `region` into `out_dir`; return it.

    Run this at build time, where the network is available: it downloads the OSM
    extract, parses the road graph, and computes the CCH contraction order (the
    slow, machine-independent steps), then writes everything to out_dir. At
    runtime, pass data_dir=out_dir to road_router/drive/region_connector to load
    it with no network and no re-parsing.
    """
    from travelplanner.graph.road import CCHRoadRouter
    from travelplanner.graph.road.osm import load_road_graph
    from travelplanner.graph.road.store import save_road_artifact

    graph = load_road_graph(download_region(region), store_names=False)
    router = CCHRoadRouter(graph)  # computes the contraction order
    return save_road_artifact(graph, router.order, out_dir)


def region_connector(region: str, stops, *, data_dir: str | None = None, **kwargs):
    """A street-accurate CCHConnector backed by the region's road network."""
    from travelplanner.graph.coupling import CCHConnector

    return CCHConnector(road_router(region, data_dir), stops, **kwargs)


def _coerce(point) -> Location:
    if isinstance(point, Location):
        return point
    if isinstance(point, (tuple, list)) and len(point) == 2:
        return Location(f"{point[0]},{point[1]}", LocationType.LANDMARK,
                        float(point[0]), float(point[1]))
    text = str(point).strip()
    if "," in text:
        a, b = (s.strip() for s in text.split(",", 1))
        try:
            lat, lon = float(a), float(b)
        except ValueError:
            lat = None
        if lat is not None:
            # Parses as a coordinate; Location validates the range (and raises a
            # clear out-of-range error rather than falling back to a city lookup).
            return Location(text, LocationType.LANDMARK, lat, lon)
    lat, lon = resolve_city(text)
    return Location(text, LocationType.CITY, lat, lon)


# A point further than this from any road node is not covered by the region's
# data; snapping it anyway would give a misleading answer.
MAX_SNAP_KM = 25.0


def _snap(router, point: Location, region: str) -> int:
    idx, dist = router.node_grid.nearest(point.lat, point.lon)
    if dist > MAX_SNAP_KM:
        raise ValueError(
            f"{point.name!r} ({point.lat},{point.lon}) is not within the "
            f"{region!r} road data (nearest road is {dist:.0f} km away). "
            f"Use a region that covers it.")
    return idx


def _path_distance_km(graph, node_indices) -> float:
    total = 0.0
    for a, b in zip(node_indices, node_indices[1:]):
        total += haversine(graph.latitude[a], graph.longitude[a],
                           graph.latitude[b], graph.longitude[b])
    return total


def drive(origin, dest, region: str, *, day: date | None = None,
          conditions: frozenset = frozenset(),
          data_dir: str | None = None) -> DriveResult:
    """Street-accurate driving estimate, or drivable=False if no road connects.

    With data_dir, route over a prebuilt offline artifact (see build_region)
    rather than downloading and building the region.
    """
    o = _coerce(origin)
    d = _coerce(dest)
    router = road_router(region, data_dir)
    g = router.graph
    road = router.customized(day or date.today(), conditions)
    path = road.route_index(_snap(router, o, region), _snap(router, d, region))
    if path is None:
        return DriveResult(drivable=False)
    return DriveResult(drivable=True,
                       duration=timedelta(seconds=path.seconds),
                       distance_km=round(_path_distance_km(g, path.node_indices), 1))
