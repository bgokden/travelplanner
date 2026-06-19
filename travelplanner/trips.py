"""Door-to-door multimodal trip planning (the high-level entry point).

`plan_trip(origin, dest, depart_at, timetable, ...)` takes TWO locations (names,
"lat,lon" strings, (lat, lon) tuples, or Locations) and returns ranked
door-to-door itineraries: ground access -> scheduled line-haul (rail/ferry/
flight via CSA) -> ground egress, with a pure-ground candidate, selected on a
Pareto frontier over (time, cost, transfers) and ordered by the objective.

This is GLUE over the existing engine: geocode the endpoints (`_coerce`), pick a
connector, and call `plan()`. The default access/egress is the region-free
`GeometricConnector` (straight-line + speed); `road=True` upgrades to a road
network connector when ONE Geofabrik region covers both endpoints, and falls
back to geometric otherwise (a cross-border trip never silently loads a country
extract). The caller may pass an explicit `connector=` for full control.
"""

from datetime import datetime

from travelplanner.geo import haversine
from travelplanner.models import Itinerary, Location
from travelplanner.graph.coupling import GeometricConnector, RoadConnector
from travelplanner.graph.coupling.planner import plan
from travelplanner.graph.query import Objective
from travelplanner.graph.scheduled.model import Timetable
from travelplanner.roads import _auto_region, _coerce, region_connector

# Stops further than this from both endpoints cannot be an access/egress point,
# so they are dropped before the (expensive) road-node snapping in CCHConnector.
# Matches CCHConnector's default max_access_km (60 km) plus a small margin.
ROAD_ACCESS_KM = 60.0
_ACCESS_MARGIN_KM = 5.0


def _nearby_stops(timetable: Timetable, points, max_km: float) -> dict:
    """Stops within max_km of ANY of the given points (origin/dest), by haversine.

    The road connector snaps each stop it is given to a road node at
    construction; filtering first keeps that O(stops) work proportional to the
    trip, not to the whole (possibly national) feed.
    """
    return {sid: stop for sid, stop in timetable.stops.items()
            if any(haversine(p.lat, p.lon, stop.lat, stop.lon) <= max_km
                   for p in points)}


def _road_connector(origin: Location, dest: Location, timetable: Timetable,
                    region, data_dir, turn_aware):
    """A road-backed connector when one region covers both endpoints, else None.

    Returns None (caller falls back to geometric) when the endpoints are
    cross-border/across water -- `_auto_region` raises there, by design, and we
    must not silently load a country-scale extract. turn_aware backs the
    connector with the edge-expanded, turn-correct router.
    """
    coords = [(origin.lat, origin.lon), (dest.lat, dest.lon)]
    try:
        resolved = _auto_region(region, data_dir, coords)
    except ValueError:
        return None
    nearby = _nearby_stops(timetable, [origin, dest],
                           ROAD_ACCESS_KM + _ACCESS_MARGIN_KM)
    return region_connector(resolved, nearby, data_dir=data_dir,
                            turn_aware=turn_aware)


def plan_trip(origin, dest, depart_at: datetime, timetable: Timetable, *,
              objective: Objective = Objective.AIR_PRIORITY, top_n: int = 3,
              conditions: frozenset = frozenset(), geocoder=None,
              road: bool = False, turn_aware: bool = False,
              region: str | None = None, data_dir: str | None = None,
              connector: RoadConnector | None = None) -> list[Itinerary]:
    """Rank door-to-door multimodal itineraries between two locations.

    origin/dest accept the same forms as `drive()`: a Location, a (lat, lon)
    tuple, a "lat,lon" string, or a place name (resolved via the active geocoder,
    or a per-call `geocoder=`). `timetable` is a GTFS Timetable
    (`load_timetable(feed_dir)` or `sample_timetable()`); transit quality is feed
    quality, and feeds are not auto-discovered (you supply one).

    Connector selection: an explicit `connector=` wins. Else with `road=True` and
    a single Geofabrik region covering both endpoints, access/egress/direct use
    that road network (`region`/`data_dir` pin or load it offline); a cross-border
    trip falls back to the region-free `GeometricConnector`. Without `road`, the
    default is `GeometricConnector` (straight-line + speed, not land-route-aware).

    `turn_aware=True` (only with `road=True`) backs the road connector with the
    edge-expanded, turn-correct router so driving legs honour turn restrictions
    and junction/signal costs. It needs signal + restriction data: a `data_dir`
    built with `build_region(..., turn_aware=True)`, or an online parse.

    Returns up to `top_n` Itinerary objects, best first for `objective`. An EMPTY
    list means no route exists (not an error); an invalid coordinate raises.
    """
    o = _coerce(origin, geocoder=geocoder)
    d = _coerce(dest, geocoder=geocoder)

    if turn_aware and not road:
        raise ValueError("turn_aware=True requires road=True (it tunes the road "
                         "connector; the geometric connector has no turns).")

    if connector is None:
        if road:
            connector = _road_connector(o, d, timetable, region, data_dir,
                                        turn_aware)
        if connector is None:
            connector = GeometricConnector(timetable.stops)

    return plan(o, d, depart_at, timetable, connector,
                conditions=conditions, objective=objective, top_n=top_n)
