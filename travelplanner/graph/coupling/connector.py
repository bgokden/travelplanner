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
from datetime import datetime
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


def _ground_leg(distance_km: float, *, drive_kmh: float, walk_kmh: float,
                walk_threshold_km: float, detour: float) -> AccessLeg:
    if distance_km <= walk_threshold_km:
        seconds = distance_km / walk_kmh * 3600.0
        return AccessLeg(Mode.WALK, seconds, distance_km, CostLevel.LOW)
    road_km = distance_km * detour
    seconds = road_km / drive_kmh * 3600.0
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


class CCHConnector:
    """Road access/egress via the Phase 1 CCH engine.

    `router` is a CCHRoadRouter (duck-typed; not imported here to avoid pulling
    in routingkit when only the geometric connector is used). `stop_to_node`
    maps each stop id to a road-graph node key; stops without a mapping are
    snapped to the nearest road node by coordinates.
    """

    def __init__(self, router, stops: dict[str, Stop],
                 stop_to_node: dict[str, str] | None = None, *,
                 max_access_km: float = 60.0,
                 drive_kmh: float = 60.0) -> None:
        self.router = router
        self.stops = stops
        self.max_access_km = max_access_km
        self.drive_kmh = drive_kmh
        self._stop_node = dict(stop_to_node or {})
        for sid, stop in stops.items():
            self._stop_node.setdefault(sid, self._nearest_node(stop.lat, stop.lon))
        self._customized: dict[frozenset[str], object] = {}

    def _nearest_node(self, lat: float, lon: float) -> str:
        idx, _ = self.router.node_grid.nearest(lat, lon)
        return self.router.graph.key(idx)

    def _road(self, conditions: frozenset[str], day):
        # cache one customized metric per (conditions); the planner uses one day.
        key = conditions
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
            if haversine(point.lat, point.lon, stop.lat, stop.lon) > self.max_access_km:
                continue
            node = self._stop_node[sid]
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
        o = self._nearest_node(origin.lat, origin.lon)
        d = self._nearest_node(dest.lat, dest.lon)
        secs = self._seconds(o, d, conditions, day)
        if secs is None:
            return None
        return AccessLeg(Mode.CAR, secs, secs / 3600.0 * self.drive_kmh,
                         CostLevel.MEDIUM)
