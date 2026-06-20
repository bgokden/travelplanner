"""Street-accurate driving via on-demand OpenStreetMap extracts.

The first time you route in a region, its OpenStreetMap extract is downloaded
from Geofabrik and cached on disk; a Customizable Contraction Hierarchies index
is then built (in memory, per process). After the first download everything is
offline and fast.

    drive(origin, dest, region="switzerland")
      -> DriveResult(drivable=True, duration=..., distance_km=...)
      -> DriveResult(drivable=False)   when no road connects them

The road engine (routingkit-cch + osmium) is a core dependency, so this works
out of the box; it just needs internet for the first download of each region.
`region` may be a known name, a Geofabrik URL, or a local .osm.pbf path.

For offline deployment, prepare a region at build time and load it with no
network at runtime:

    build_region("switzerland", out_dir)          # build step (needs internet)
    drive(origin, dest, "switzerland", data_dir=out_dir)   # runtime (offline)

build_region writes the parsed road graph and the CCH contraction order (the
slow, machine-independent steps) to out_dir; data_dir loads them back, skipping
the download, the OSM parse, and the order computation.

Concurrency: a router and its customized roads wrap native (Rust) routing
objects that are thread-affine -- do not share one across threads (it raises a
low-level panic, not a clean Python exception). For parallel batch work use
separate processes (each loads the same on-disk artifact cheaply), or use
drive_matrix on one thread, which reuses a single customized metric. Within a
process, road_router caches one router per region and customized() caches the
metric per (day, conditions), so sequential calls are fast.
"""

import os
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache

from travelplanner.geocoding import resolve_city
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
    # Fall back to the full Geofabrik catalog if its index is already cached
    # locally (no network here -- run list_regions()/geofabrik.catalog() once to
    # populate it). This widens coverage from the curated REGIONS to all ~555
    # extracts without forcing a download on every unknown name.
    from travelplanner.geofabrik import cached_catalog
    entry = cached_catalog().get(key)
    if entry is not None:
        return entry.pbf_url
    raise ValueError(
        f"Unknown region {region!r}. Use a known name "
        f"({', '.join(sorted(REGIONS))}), any Geofabrik catalog id "
        f"(see list_regions()), a Geofabrik URL, or a local .osm.pbf path.")


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


@lru_cache(maxsize=2)
def _expanded_router_cached(region: str, data_dir: str | None):
    from travelplanner.graph.road.expanded import ExpandedCCHRoadRouter
    from travelplanner.graph.road.turns import TurnCosts, build_expanded_graph

    if data_dir is not None:
        # offline artifact: base graph carries signals + restrictions (v3), and
        # the expanded contraction order is loaded so the CCH builds instantly.
        from travelplanner.graph.road.store import (load_expanded_order,
                                                    load_road_artifact)
        base, _ = load_road_artifact(data_dir)
        expanded = build_expanded_graph(base, turn_costs=TurnCosts())
        return ExpandedCCHRoadRouter(expanded, order=load_expanded_order(data_dir))

    # online: turn-aware needs signal + turn-restriction data, so load it
    from travelplanner.graph.road.osm import load_road_graph
    base = load_road_graph(download_region(region), store_names=False,
                           turn_data=True)
    expanded = build_expanded_graph(base, turn_costs=TurnCosts())
    return ExpandedCCHRoadRouter(expanded)


def _router_for(region, data_dir, turn_aware):
    """The node-based router, or the turn-aware (edge-expanded) one if requested."""
    if turn_aware:
        return _expanded_router_cached(region, data_dir)
    return road_router(region, data_dir)


def build_region(region: str, out_dir: str, *, turn_aware: bool = False) -> str:
    """Build an offline road artifact for `region` into `out_dir`; return it.

    Run this at build time, where the network is available: it downloads the OSM
    extract, parses the road graph, and computes the CCH contraction order (the
    slow, machine-independent steps), then writes everything to out_dir. At
    runtime, pass data_dir=out_dir to road_router/drive/region_connector to load
    it with no network and no re-parsing.

    turn_aware=True additionally parses signals + turn restrictions and computes
    the turn-expanded contraction order, so drive(..., turn_aware=True,
    data_dir=out_dir) loads instantly offline instead of re-parsing and
    re-expanding (the slow, ~minutes step).
    """
    from travelplanner.graph.road import CCHRoadRouter
    from travelplanner.graph.road.osm import load_road_graph
    from travelplanner.graph.road.store import save_road_artifact

    graph = load_road_graph(download_region(region), store_names=False,
                            turn_data=turn_aware)
    router = CCHRoadRouter(graph)  # computes the contraction order
    save_road_artifact(graph, router.order, out_dir)
    if turn_aware:
        from travelplanner.graph.road.expanded import ExpandedCCHRoadRouter
        from travelplanner.graph.road.store import save_expanded_order
        from travelplanner.graph.road.turns import TurnCosts, build_expanded_graph
        expanded = build_expanded_graph(graph, turn_costs=TurnCosts())
        save_expanded_order(ExpandedCCHRoadRouter(expanded).order, out_dir)
    return out_dir


def region_connector(region: str, stops, *, data_dir: str | None = None,
                     turn_aware: bool = False, **kwargs):
    """A street-accurate CCHConnector backed by the region's road network.

    turn_aware=True backs it with the edge-expanded, turn-aware router (turn
    restrictions + junction/signal costs), so access/egress/direct driving times
    are turn-correct. It needs signal + restriction data: pass a data_dir built
    with build_region(..., turn_aware=True), or omit data_dir to parse online.
    """
    from travelplanner.graph.coupling import CCHConnector

    return CCHConnector(_router_for(region, data_dir, turn_aware), stops, **kwargs)


def _coerce(point, *, geocoder=None) -> Location:
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
            # clear out-of-range error rather than falling back to a name lookup).
            return Location(text, LocationType.LANDMARK, lat, lon)
    lat, lon = resolve_city(text, geocoder=geocoder)
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


def _drive_between(road, graph, from_idx: int, to_idx: int) -> DriveResult:
    if from_idx == to_idx:
        return DriveResult(drivable=True, duration=timedelta(0), distance_km=0.0)
    path = road.route_index(from_idx, to_idx)
    if path is None:
        return DriveResult(drivable=False)
    return DriveResult(drivable=True,
                       duration=timedelta(seconds=path.seconds),
                       distance_km=round(_path_distance_km(graph, path.node_indices), 1))


def _auto_region(region, data_dir, coords):
    """Resolve region: keep an explicit one, else auto-select from coordinates.

    With data_dir (offline) the region is just a label, so a None is left as-is.
    """
    if region is not None or data_dir is not None:
        return region
    from travelplanner.geofabrik import region_for_points
    return region_for_points(coords).pbf_url


@dataclass(frozen=True)
class Route(DriveResult):
    """A DriveResult plus the routed geometry as (lat, lon) points along the path."""
    geometry: tuple = ()

    def to_geojson(self) -> dict:
        """A GeoJSON Feature (LineString, [lon, lat] order) for map rendering."""
        return {
            "type": "Feature",
            "properties": {"drivable": self.drivable,
                           "duration_s": (self.duration.total_seconds()
                                          if self.duration is not None else None),
                           "distance_km": self.distance_km},
            "geometry": {"type": "LineString",
                         "coordinates": [[lon, lat] for lat, lon in self.geometry]},
        }


def drive_route(origin, dest, region: str | None = None, *,
                day: date | None = None, depart_at: datetime | None = None,
                conditions: frozenset = frozenset(),
                data_dir: str | None = None, geocoder=None,
                speed_model=None, turn_aware: bool = False) -> Route:
    """Like drive(), but also returns the routed path geometry (see Route).

    Use route.to_geojson() or travelplanner.viz.save_route_map(route, path).
    turn_aware=True routes over the edge-expanded, turn-aware graph.
    """
    o = _coerce(origin, geocoder=geocoder)
    d = _coerce(dest, geocoder=geocoder)
    region = _auto_region(region, data_dir, [(o.lat, o.lon), (d.lat, d.lon)])
    router = _router_for(region, data_dir, turn_aware)
    g = router.graph
    when_day = (depart_at.date() if depart_at is not None else day) or date.today()
    road = router.customized(when_day, conditions, depart_at=depart_at,
                             speed_model=speed_model)
    oi, di = _snap(router, o, region), _snap(router, d, region)
    if oi == di:
        pt = (g.latitude[oi], g.longitude[oi])
        return Route(True, timedelta(0), 0.0, geometry=(pt,))
    path = road.route_index(oi, di)
    if path is None:
        return Route(False)
    geometry = tuple((g.latitude[i], g.longitude[i]) for i in path.node_indices)
    return Route(True, timedelta(seconds=path.seconds),
                 round(_path_distance_km(g, path.node_indices), 1),
                 geometry=geometry)


def drive(origin, dest, region: str | None = None, *, day: date | None = None,
          depart_at: datetime | None = None,
          conditions: frozenset = frozenset(),
          data_dir: str | None = None, geocoder=None,
          speed_model=None, turn_aware: bool = False) -> DriveResult:
    """Street-accurate driving estimate, or drivable=False if no road connects.

    origin/dest may be a Location, (lat, lon), "lat,lon", or a place name
    (resolved via the active or supplied geocoder). region is optional: when
    omitted it is auto-selected as the smallest Geofabrik extract covering both
    endpoints (raises for a cross-border trip no single extract covers). Times
    use the active speed model, which decides automatically: average by default,
    or time-of-day (rush-hour) effects when you pass depart_at; pass speed_model
    to override (e.g. free_flow_model). turn_aware=True routes over the
    edge-expanded turn-aware graph. With data_dir, route over a prebuilt offline
    artifact (see build_region).
    """
    o = _coerce(origin, geocoder=geocoder)
    d = _coerce(dest, geocoder=geocoder)
    region = _auto_region(region, data_dir, [(o.lat, o.lon), (d.lat, d.lon)])
    router = _router_for(region, data_dir, turn_aware)
    when_day = (depart_at.date() if depart_at is not None else day) or date.today()
    road = router.customized(when_day, conditions, depart_at=depart_at,
                             speed_model=speed_model)
    return _drive_between(road, router.graph,
                          _snap(router, o, region), _snap(router, d, region))


def drive_matrix(points, region: str | None = None, *, dests=None,
                 day: date | None = None,
                 depart_at: datetime | None = None,
                 conditions: frozenset = frozenset(),
                 data_dir: str | None = None, geocoder=None,
                 speed_model=None) -> list[list[DriveResult]]:
    """Driving results for every origin x destination pair.

    Reuses one customized road metric and snaps each point once, so it is far
    cheaper than calling drive() per pair. Returns a list of rows (one per
    origin); each entry is a DriveResult (drivable=False when no road connects,
    duration/distance 0 on the diagonal). With dests=None the square
    points x points matrix is computed; otherwise points are origins and dests
    the destinations. Results are direction-dependent (not symmetric).
    """
    origins = [_coerce(p, geocoder=geocoder) for p in points]
    dest_locs = origins if dests is None else [_coerce(p, geocoder=geocoder)
                                               for p in dests]
    region = _auto_region(region, data_dir,
                          [(p.lat, p.lon) for p in origins + dest_locs])
    router = road_router(region, data_dir)
    g = router.graph
    when_day = (depart_at.date() if depart_at is not None else day) or date.today()
    road = router.customized(when_day, conditions, depart_at=depart_at,
                             speed_model=speed_model)
    origin_idx = [_snap(router, p, region) for p in origins]
    dest_idx = (origin_idx if dests is None
                else [_snap(router, p, region) for p in dest_locs])
    return [[_drive_between(road, g, oi, di) for di in dest_idx]
            for oi in origin_idx]
