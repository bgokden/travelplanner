"""Road connectors: ground access/egress between locations and transit stops.

A RoadConnector answers three questions for the coupling planner:
- access: from the origin, which stops can I reach by road/walk, and how long?
- egress: from each stop, how long to the destination by road/walk?
- direct: pure ground origin -> destination (the no-transit candidate).

GeometricConnector uses haversine + speeds (lightweight, deterministic, no
routingkit). CCHConnector wires the Phase 1 CCH engine with nearest-node
snapping. Both expose the same methods so the planner is connector-agnostic.
"""

from dataclasses import dataclass
from datetime import date
from typing import Protocol

from travelplanner.geo import haversine
from travelplanner.models import CostLevel, Location, Mode
from travelplanner.graph.scheduled.model import Stop


@dataclass(frozen=True)
class AccessLeg:
    mode: Mode
    seconds: float
    distance_km: float
    cost_level: CostLevel


class RoadConnector(Protocol):
    def access(self, origin: Location, conditions: frozenset[str],
               *, day=None) -> dict[str, AccessLeg]: ...

    def egress(self, dest: Location, conditions: frozenset[str],
               *, day=None) -> dict[str, AccessLeg]: ...

    def direct(self, origin: Location, dest: Location,
               conditions: frozenset[str], *, day=None) -> AccessLeg | None: ...


# Marginal driving speeds by distance band: the first urban/access kilometres run
# at the base speed, then regional, then motorway. Each tuple is (band upper bound
# km, band speed km/h); a band speed below the base is lifted to the base.
_DRIVE_BANDS = ((30.0, 0.0), (150.0, 80.0), (float("inf"), 100.0))


def _drive_seconds(road_km: float, base_kmh: float) -> float:
    """Drive time integrated over the distance bands (not a single speed picked by
    total length). Picking one speed by total distance made time discontinuous and
    NON-monotonic -- a leg just past a band boundary (e.g. 100 km/h above 150 km)
    came out faster than a shorter one (80 km/h below it). Integrating per band
    keeps drive time continuous and strictly increasing in distance."""
    seconds = 0.0
    lower = 0.0
    for upper, band_kmh in _DRIVE_BANDS:
        if road_km <= lower:
            break
        band_km = min(road_km, upper) - lower
        seconds += band_km / max(base_kmh, band_kmh) * 3600.0
        lower = upper
    return seconds


def _ground_leg(distance_km: float, *, drive_kmh: float, walk_kmh: float,
                walk_threshold_km: float, detour: float) -> AccessLeg:
    if distance_km <= walk_threshold_km:
        seconds = distance_km / walk_kmh * 3600.0
        return AccessLeg(Mode.WALK, seconds, distance_km, CostLevel.LOW)
    road_km = distance_km * detour
    seconds = _drive_seconds(road_km, drive_kmh)
    return AccessLeg(Mode.CAR, seconds, road_km, CostLevel.MEDIUM)


class GeometricConnector:
    """Straight-line + speed model. Conditions are ignored (no seasonal roads).

    WARNING: this connector is NOT land-route-aware. It estimates ground access
    from straight-line distance, so it will happily return a car/walk leg across
    water or impassable terrain, and it cannot represent seasonal or conditional
    road availability. For feasibility and seasonality (e.g. an island reachable
    only by a summer ferry), use a road-backed connector (CCHConnector /
    travelplanner.roads.region_connector) over a real road graph.
    """

    def __init__(self, stops: dict[str, Stop], *, max_access_km: float = 50.0,
                 max_ground_km: float = 1500.0,
                 drive_kmh: float = 60.0, walk_kmh: float = 5.0,
                 walk_threshold_km: float = 1.5, detour: float = 1.3) -> None:
        self.stops = stops
        self.max_access_km = max_access_km
        # Straight-line distance is not land-route-aware, so a pure-ground
        # candidate is only offered within a plausible range; beyond it, ground
        # would mean "drive across the ocean". (Access/egress are bounded by
        # max_access_km already.)
        self.max_ground_km = max_ground_km
        self.drive_kmh = drive_kmh
        self.walk_kmh = walk_kmh
        self.walk_threshold_km = walk_threshold_km
        self.detour = detour

    def _leg_to(self, a: Location | Stop, b: Location | Stop) -> AccessLeg:
        d = haversine(a.lat, a.lon, b.lat, b.lon)
        return _ground_leg(d, drive_kmh=self.drive_kmh, walk_kmh=self.walk_kmh,
                           walk_threshold_km=self.walk_threshold_km,
                           detour=self.detour)

    def _nearby(self, point: Location) -> dict[str, AccessLeg]:
        out: dict[str, AccessLeg] = {}
        for sid, stop in self.stops.items():
            if haversine(point.lat, point.lon, stop.lat, stop.lon) <= self.max_access_km:
                out[sid] = self._leg_to(point, stop)
        return out

    def access(self, origin: Location, conditions: frozenset[str] = frozenset(),
               *, day=None) -> dict[str, AccessLeg]:
        return self._nearby(origin)

    def egress(self, dest: Location, conditions: frozenset[str] = frozenset(),
               *, day=None) -> dict[str, AccessLeg]:
        return self._nearby(dest)

    def direct(self, origin: Location, dest: Location,
               conditions: frozenset[str] = frozenset(), *,
               day=None) -> AccessLeg | None:
        if haversine(origin.lat, origin.lon, dest.lat, dest.lon) > self.max_ground_km:
            return None
        return self._leg_to(origin, dest)


class SplitConnector:
    """Composite RoadConnector with separate per-endpoint connectors.

    For a trip whose endpoints fall in DIFFERENT road regions (no single
    Geofabrik extract covers both, e.g. Zaandam -> Maastricht), access at the
    origin is resolved by `access_connector` (the origin's region) and egress at
    the destination by `egress_connector` (the destination's region). The
    pure-ground `direct` candidate is delegated to an optional `direct_connector`
    (e.g. a GeometricConnector): neither regional graph spans both endpoints, so a
    road direct is undefined; without one, there is simply no ground candidate.
    """

    def __init__(self, access_connector: RoadConnector,
                 egress_connector: RoadConnector, *,
                 direct_connector: RoadConnector | None = None) -> None:
        self._access = access_connector
        self._egress = egress_connector
        self._direct = direct_connector

    def access(self, origin: Location, conditions: frozenset[str] = frozenset(),
               *, day=None) -> dict[str, AccessLeg]:
        return self._access.access(origin, conditions, day=day)

    def egress(self, dest: Location, conditions: frozenset[str] = frozenset(),
               *, day=None) -> dict[str, AccessLeg]:
        return self._egress.egress(dest, conditions, day=day)

    def direct(self, origin: Location, dest: Location,
               conditions: frozenset[str] = frozenset(), *,
               day=None) -> AccessLeg | None:
        if self._direct is None:
            return None
        return self._direct.direct(origin, dest, conditions, day=day)


class CCHConnector:
    """Road access/egress via the Phase 1 CCH engine.

    `router` is a CCHRoadRouter (duck-typed; not imported here to avoid pulling
    in routingkit when only the geometric connector is used). `stop_to_node`
    maps each stop id to a road-graph node key; stops without a mapping are
    snapped to the nearest road node by coordinates. A hop within
    `walk_threshold_km` is returned as a WALK leg (you would not drive 300 m),
    matching GeometricConnector; longer hops are routed on the road network.
    """

    def __init__(self, router, stops: dict[str, Stop],
                 stop_to_node: dict[str, str] | None = None, *,
                 max_access_km: float = 60.0, max_snap_km: float = 25.0,
                 drive_kmh: float = 60.0, walk_kmh: float = 5.0,
                 walk_threshold_km: float = 1.5) -> None:
        self.router = router
        self.stops = stops
        self.max_access_km = max_access_km
        self.max_snap_km = max_snap_km
        self.drive_kmh = drive_kmh
        self.walk_kmh = walk_kmh
        self.walk_threshold_km = walk_threshold_km
        self._stop_node = dict(stop_to_node or {})
        for sid, stop in stops.items():
            if sid in self._stop_node:
                continue
            node = self._nearest_node(stop.lat, stop.lon)
            if node is not None:        # a stop outside the road coverage has none
                self._stop_node[sid] = node
        self._customized: dict[tuple, object] = {}

    def _walk_leg(self, dist_km: float) -> AccessLeg:
        return AccessLeg(Mode.WALK, dist_km / self.walk_kmh * 3600.0, dist_km,
                         CostLevel.LOW)

    def _nearest_node(self, lat: float, lon: float) -> str | None:
        # None when the point is beyond max_snap_km of any road node (outside the
        # region's coverage) -- snapping it anyway would fabricate a bogus drive,
        # mirroring the MAX_SNAP_KM guard in roads._snap.
        idx, dist = self.router.node_grid.nearest(lat, lon)
        if idx < 0 or dist > self.max_snap_km:
            return None
        return self.router.graph.key(idx)

    def _road(self, conditions: frozenset[str], day):
        # cache one customized metric per (conditions, day): seasonal / day-of-week
        # validity makes the road metric date-dependent, so a connector reused
        # across dates must not serve the first day's metric for a later day.
        # day=None means "current conditions" (seasonal validity needs a date),
        # matching drive_route; the RoadConnector protocol allows it.
        if day is None:
            day = date.today()
        key = (conditions, day)
        road = self._customized.get(key)
        if road is None:
            road = self.router.customize(day, conditions)
            self._customized[key] = road
        return road

    def _seconds(self, from_node: str, to_node: str, conditions, day) -> float | None:
        if from_node == to_node:
            return 0.0
        path = self._road(conditions, day).route(from_node, to_node)
        return None if path is None else float(path.seconds)

    def _legs_from_point(self, point: Location, conditions, day,
                         to_dest: bool) -> dict[str, AccessLeg]:
        origin_node = self._nearest_node(point.lat, point.lon)
        out: dict[str, AccessLeg] = {}
        for sid, stop in self.stops.items():
            d_km = haversine(point.lat, point.lon, stop.lat, stop.lon)
            if d_km > self.max_access_km:
                continue
            if d_km <= self.walk_threshold_km:     # a short hop is walked, not driven
                out[sid] = self._walk_leg(d_km)
                continue
            node = self._stop_node.get(sid)
            if node is None or origin_node is None:   # not within the road coverage
                continue
            a, b = (node, origin_node) if to_dest else (origin_node, node)
            secs = self._seconds(a, b, conditions, day)
            if secs is None:
                continue
            dist_km = secs / 3600.0 * self.drive_kmh
            out[sid] = AccessLeg(Mode.CAR, secs, dist_km, CostLevel.MEDIUM)
        return out

    def access(self, origin: Location,
               conditions: frozenset[str] = frozenset(), *,
               day=None) -> dict[str, AccessLeg]:
        return self._legs_from_point(origin, conditions, day, to_dest=False)

    def egress(self, dest: Location,
               conditions: frozenset[str] = frozenset(), *,
               day=None) -> dict[str, AccessLeg]:
        return self._legs_from_point(dest, conditions, day, to_dest=True)

    def direct(self, origin: Location, dest: Location,
               conditions: frozenset[str] = frozenset(), *,
               day=None) -> AccessLeg | None:
        d_km = haversine(origin.lat, origin.lon, dest.lat, dest.lon)
        if d_km <= self.walk_threshold_km:         # a short hop is walked, not driven
            return self._walk_leg(d_km)
        o = self._nearest_node(origin.lat, origin.lon)
        d = self._nearest_node(dest.lat, dest.lon)
        if o is None or d is None:                 # an endpoint outside the coverage
            return None
        secs = self._seconds(o, d, conditions, day)
        if secs is None:
            return None
        return AccessLeg(Mode.CAR, secs, secs / 3600.0 * self.drive_kmh,
                         CostLevel.MEDIUM)
