"""Door-to-door multimodal trip planning (the high-level entry point).

`plan_trip(origin, dest, depart_at, timetable, ...)` takes TWO locations (names,
"lat,lon" strings, (lat, lon) tuples, or Locations) and returns ranked
door-to-door itineraries: ground access -> scheduled line-haul (rail/ferry/
flight via CSA) -> ground egress, with a pure-ground candidate, selected on a
Pareto frontier over (time, cost, transfers) and ordered by the objective.

This is GLUE over the existing engine: geocode the endpoints (`_coerce`), pick the
connector(s), and call `plan_multi()`. The default access/egress is the region-free
`GeometricConnector` (straight-line + speed); `road=True` upgrades to a road
network connector when ONE Geofabrik region covers both endpoints, and falls
back to geometric otherwise (a cross-border trip never silently loads a country
extract). The caller may pass an explicit `connector=` for full control.
"""

from datetime import datetime

from travelplanner.geo import haversine
from travelplanner.models import Itinerary, Location
from travelplanner.graph.coupling import (
    GeometricConnector,
    RoadConnector,
    SplitConnector,
)
from travelplanner.graph.coupling.planner import plan_multi
from travelplanner.graph.query import Objective
from travelplanner.graph.scheduled.model import Timetable
from travelplanner.roads import _auto_region, _coerce, region_connector

# Stops further than this from both endpoints cannot be an access/egress point,
# so they are dropped before the (expensive) road-node snapping in CCHConnector.
# Matches CCHConnector's default max_access_km (60 km) plus a small margin.
ROAD_ACCESS_KM = 60.0
_ACCESS_MARGIN_KM = 5.0

# Transit access: walk to a stop within this radius and let the scheduled network
# cover longer hops (e.g. the local train to the airport). Beyond it, a stop is
# not an access point -- you reach it as a line-haul leg, not on foot.
WALK_ACCESS_KM = 2.0


def _nearby_stops(timetable: Timetable, points, max_km: float) -> dict:
    """Stops within max_km of ANY of the given points (origin/dest), by haversine.

    The road connector snaps each stop it is given to a road node at
    construction; filtering first keeps that O(stops) work proportional to the
    trip, not to the whole (possibly national) feed.
    """
    return {sid: stop for sid, stop in timetable.stops.items()
            if any(haversine(p.lat, p.lon, stop.lat, stop.lon) <= max_km
                   for p in points)}


def _split_connector(origin: Location, dest: Location, timetable: Timetable,
                     turn_aware):
    """A SplitConnector with per-endpoint regions, or None if either endpoint is
    not covered. Only used online (no data_dir): a single data_dir cannot hold
    two regions, so an offline cross-region trip falls back to geometric.
    """
    from travelplanner.geofabrik import region_for

    try:
        acc_region = region_for(origin.lat, origin.lon)
        egr_region = region_for(dest.lat, dest.lon)
    except ValueError:
        return None
    margin = ROAD_ACCESS_KM + _ACCESS_MARGIN_KM
    access = region_connector(acc_region.pbf_url,
                              _nearby_stops(timetable, [origin], margin),
                              turn_aware=turn_aware)
    egress = region_connector(egr_region.pbf_url,
                              _nearby_stops(timetable, [dest], margin),
                              turn_aware=turn_aware)
    # The cross-region drive spans no single graph; keep a geometric ground
    # candidate so the frontier still has a direct option.
    return SplitConnector(access, egress,
                          direct_connector=GeometricConnector(timetable.stops))


def _transit_connector(timetable: Timetable) -> GeometricConnector:
    """Walk-only access: reach a stop within WALK_ACCESS_KM on foot, the rest is
    line-haul (no car legs)."""
    return GeometricConnector(timetable.stops, max_access_km=WALK_ACCESS_KM,
                              walk_threshold_km=WALK_ACCESS_KM)


def _mode_connector(mode: str, timetable: Timetable) -> GeometricConnector:
    """A geometric connector for one first/last-mile mode: 'transit' is walk-only,
    'car' is the default drive/walk connector. (Callers pass a validated mode.)"""
    if mode == "transit":
        return _transit_connector(timetable)
    if mode == "car":
        return GeometricConnector(timetable.stops)
    raise ValueError(f"unknown first/last-mile mode {mode!r}")


def _road_connector(origin: Location, dest: Location, timetable: Timetable,
                    region, data_dir, turn_aware):
    """A road-backed connector for the trip, or None to fall back to geometric.

    When one region covers both endpoints, one region_connector backs the whole
    trip. When they fall in different regions (`_auto_region` raises) and we are
    online (no data_dir), a SplitConnector resolves access and egress in their
    own regions. We never silently load a country-scale extract; an offline
    cross-region trip returns None (geometric fallback). turn_aware backs each
    road connector with the edge-expanded, turn-correct router.
    """
    coords = [(origin.lat, origin.lon), (dest.lat, dest.lon)]
    try:
        resolved = _auto_region(region, data_dir, coords)
    except ValueError:
        if data_dir is not None:
            return None
        return _split_connector(origin, dest, timetable, turn_aware)
    nearby = _nearby_stops(timetable, [origin, dest],
                           ROAD_ACCESS_KM + _ACCESS_MARGIN_KM)
    return region_connector(resolved, nearby, data_dir=data_dir,
                            turn_aware=turn_aware)


def _validate_modes(access: str, egress: str | None, road: bool,
                    turn_aware: bool) -> None:
    """Reject mode combinations that cannot be honoured, with a clear reason."""
    if access not in ("car", "transit", "both"):
        raise ValueError(f"access must be 'car', 'transit', or 'both', got {access!r}")
    if egress is not None and egress not in ("car", "transit"):
        raise ValueError(f"egress must be 'car' or 'transit', got {egress!r}")
    if turn_aware and not road:
        raise ValueError("turn_aware=True requires road=True (it tunes the road "
                         "connector; the geometric connector has no turns).")
    if access == "both" and egress is not None:
        raise ValueError("access='both' already pools car and transit; it cannot "
                         "also take a separate egress mode. Use single access/"
                         "egress modes for an asymmetric trip.")
    asymmetric = egress is not None and egress != access
    if road and (access in ("transit", "both") or asymmetric):
        raise ValueError("road=True needs car access/egress on both ends; for a "
                         "transit, 'both', or asymmetric first/last mile (which are "
                         "geometric) drop road, or build connectors and pass "
                         "connector=.")


def _select_connectors(origin: Location, dest: Location, timetable: Timetable, *,
                       access: str, egress: str | None, road: bool,
                       turn_aware: bool, region, data_dir,
                       connector: RoadConnector | None):
    """The connector(s) whose candidates plan_multi pools. Usually one; two for
    access='both' (car + transit pooled on one frontier). An asymmetric trip
    (egress mode differs from access) is one SplitConnector delegating each end to
    its own mode connector."""
    if connector is not None:
        return [connector]
    if access == "both":
        return [_mode_connector("car", timetable), _mode_connector("transit", timetable)]
    if egress is not None and egress != access:
        # Direct (no-transit) ground option follows the first-mile mode, so a short
        # door-to-door hop is classified the same as the symmetric access mode.
        return [SplitConnector(_mode_connector(access, timetable),
                               _mode_connector(egress, timetable),
                               direct_connector=_mode_connector(access, timetable))]
    if access == "transit":
        return [_mode_connector("transit", timetable)]
    if road:
        backed = _road_connector(origin, dest, timetable, region, data_dir, turn_aware)
        return [backed if backed is not None else _mode_connector("car", timetable)]
    return [_mode_connector("car", timetable)]


def plan_trip(origin, dest, depart_at: datetime, timetable: Timetable, *,
              objective: Objective = Objective.AIR_PRIORITY, top_n: int = 3,
              conditions: frozenset = frozenset(), geocoder=None,
              road: bool = False, turn_aware: bool = False,
              access: str = "car", egress: str | None = None,
              region: str | None = None, data_dir: str | None = None,
              connector: RoadConnector | None = None) -> list[Itinerary]:
    """Rank door-to-door multimodal itineraries between two locations.

    origin/dest accept the same forms as `drive()`: a Location, a (lat, lon)
    tuple, a "lat,lon" string, or a place name (resolved via the active geocoder,
    or a per-call `geocoder=`). `timetable` is a GTFS Timetable
    (`load_timetable(feed_dir)` or `sample_timetable()`); transit quality is feed
    quality, and feeds are not auto-discovered (you supply one).

    Connector selection: an explicit `connector=` wins and fully defines access/
    egress, so `road`/`access`/`egress` are then ignored. Else with `road=True` and
    a single Geofabrik region covering both endpoints, access/egress/direct use
    that road network (`region`/`data_dir` pin or load it offline); a cross-border
    trip falls back to the region-free `GeometricConnector`. Without `road`, the
    default is `GeometricConnector` (straight-line + speed, not land-route-aware).

    `turn_aware=True` (only with `road=True`) backs the road connector with the
    edge-expanded, turn-correct router so driving legs honour turn restrictions
    and junction/signal costs. It needs signal + restriction data: a `data_dir`
    built with `build_region(..., turn_aware=True)`, or an online parse.

    `access` selects the first-mile mode (like a "Driving" vs "Transit" tab).
    "car" (default) drives/walks to the nearest stop. "transit" only walks to a
    stop within a short radius, so longer hops (e.g. the train to the airport) go
    via the scheduled network -- a no-car door-to-door trip. "both" pools the car
    and transit candidates onto one frontier, so a drive-to-airport itinerary and
    a walk-to-train one compete and you can compare them (pair with
    `objective=GREENEST` to lead with the lower-driving one).

    `egress` overrides the last-mile mode independently of `access` (default: same
    as `access`). For example `access="transit", egress="car"` walks to the
    station for the line-haul but takes a car (rental/taxi) from the arrival stop
    to the door -- a common asymmetric trip. `egress` is "car" or "transit" (not
    "both"). Asymmetric and "transit"/"both" first/last miles are geometric, so
    `road`/`turn_aware` are rejected with them (raise; for road-backed car legs
    there, build connectors and pass `connector=`).

    Note: a "transit" access or egress reaches a stop only within a short walk; if
    no stop is in range, that end has no transit leg and the trip falls back to the
    direct ground (drive/walk) candidate -- so a transit request can still yield a
    car-only itinerary when the door is far from any stop (rather than nothing).

    Returns up to `top_n` Itinerary objects, best first for `objective`. An EMPTY
    list means no route exists (not an error); an invalid coordinate raises.
    """
    # Validate cheap mode flags before the (possibly network) geocoding, and skip
    # them entirely when an explicit connector= is supplied (it fully defines
    # access/egress, so road/access/egress are ignored -- see above).
    if connector is None:
        _validate_modes(access, egress, road, turn_aware)
    o = _coerce(origin, geocoder=geocoder)
    d = _coerce(dest, geocoder=geocoder)

    connectors = _select_connectors(o, d, timetable, access=access, egress=egress,
                                    road=road, turn_aware=turn_aware, region=region,
                                    data_dir=data_dir, connector=connector)
    return plan_multi(o, d, depart_at, timetable, connectors,
                      conditions=conditions, objective=objective, top_n=top_n)
