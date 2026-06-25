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
from travelplanner.models import Itinerary, Location, Mode
from travelplanner.graph.coupling import (
    GeometricConnector,
    RoadConnector,
    SplitConnector,
)
from travelplanner.graph.coupling.planner import plan_labeled, plan_multi
from travelplanner.graph.query import Objective
from travelplanner.graph.scheduled.model import Timetable
from travelplanner.roads import (
    _auto_region, _coerce, continent_road, region_connector)

# Stops further than this from both endpoints cannot be an access/egress point,
# so they are dropped before the (expensive) road-node snapping in CCHConnector.
# Matches CCHConnector's default max_access_km (60 km) plus a small margin.
ROAD_ACCESS_KM = 60.0
_ACCESS_MARGIN_KM = 5.0

# Transit access: walk to a stop within this radius and let the scheduled network
# cover longer hops (e.g. the local train to the airport). Beyond it, a stop is
# not an access point -- you reach it as a line-haul leg, not on foot.
WALK_ACCESS_KM = 2.0

# Named "preferred way of transportation" profiles. Each is a coherent preset of
# plan_trip arguments -- the first/last-mile `access` mode plus any `exclude_modes`
# the traveller avoids -- so a caller (or UI) picks an intent once instead of tuning
# the knobs by hand. They constrain WHICH modes appear; the `objective` (fastest,
# greenest, ...) is orthogonal and ranks whatever survives.
#   transit: walk + public transit door-to-door (no car to the station); flights
#            still allowed for long hops with no rail. The sensible default.
#   train:   like transit but suppresses flights on rail-doable corridors (shown
#            again only when flying is the only same-day option) -- "trains, not planes".
#   drive:   car-first first/last mile, every mode visible -- for a car-centric region.
#   fastest: pool car and transit access and rank purely by the objective -- no bias.
TRANSPORT_PREFERENCES = {
    "transit": {"access": "transit", "exclude_modes": frozenset()},
    "train": {"access": "transit", "exclude_modes": frozenset({Mode.FLIGHT})},
    "drive": {"access": "car", "exclude_modes": frozenset()},
    "fastest": {"access": "both", "exclude_modes": frozenset()},
}
DEFAULT_TRANSPORT_PREFERENCE = "transit"


def preference_kwargs(name: str) -> dict:
    """The plan_trip kwargs (`access`, `exclude_modes`) for a named transport
    preference. An unknown or empty name falls back to the default (transit-first),
    so a caller can pass user input straight through."""
    preset = TRANSPORT_PREFERENCES.get(name) or \
        TRANSPORT_PREFERENCES[DEFAULT_TRANSPORT_PREFERENCE]
    return dict(preset)


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
    return SplitConnector(access, egress,
                          direct_connector=_cross_region_direct(timetable))


def _cross_region_direct(timetable: Timetable):
    """The whole-trip drive for a cross-region trip: a configured continent road
    graph routes it over real highways, else a straight-line geometric estimate (no
    single per-region extract spans both endpoints)."""
    cont = continent_road()
    if cont is None:
        return GeometricConnector(timetable.stops)
    region, data_dir = cont
    # Highway-only continent graph: nodes are sparser, so allow a wider snap from
    # the door to the nearest highway. It carries no turn data, so it stays
    # node-based regardless of the trip's turn_aware setting.
    return region_connector(region, {}, data_dir=data_dir, max_snap_km=60.0)


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
    # 'both' road-backs its car arm (the transit arm stays geometric), so it is
    # allowed. A pure 'transit' or asymmetric first/last mile has no car leg to back
    # and is geometric, so road cannot apply there.
    if road and (access == "transit" or asymmetric):
        raise ValueError("road=True needs a car first/last mile; a 'transit' or "
                         "asymmetric access/egress is geometric. Drop road, or build "
                         "connectors and pass connector=.")


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
        # Pool a car arm and a (walk-only) transit arm on one frontier. With road=True
        # the car arm is road-backed (real street geometry / turn-aware) when a region
        # covers the trip, falling back to the geometric car connector otherwise; the
        # transit arm is always geometric.
        car = _road_connector(origin, dest, timetable, region, data_dir, turn_aware) if road else None
        return [car or _mode_connector("car", timetable),
                _mode_connector("transit", timetable)]
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


def _prepare_trip(origin, dest, depart_at, timetable, *, geocoder, road,
                  turn_aware, access, egress, region, data_dir, connector):
    """Shared prep for plan_trip / plan_trip_choices: validate the mode flags before
    the (possibly network) geocoding, default the departure to now, geocode the
    endpoints, auto-compose a timetable when none is given (surfacing its coverage
    notes as warnings), and select the connector(s). Returns
    (origin, dest, depart_at, timetable, connectors)."""
    # Skip mode validation when an explicit connector= is supplied: it fully defines
    # access/egress, so road/access/egress are ignored.
    if connector is None:
        _validate_modes(access, egress, road, turn_aware)
    if depart_at is None:
        depart_at = datetime.now().replace(microsecond=0)
    o = _coerce(origin, geocoder=geocoder)
    d = _coerce(dest, geocoder=geocoder)
    if timetable is None:
        import warnings
        from travelplanner.auto_timetable import build_default_timetable
        # Heads-up before the (possibly slow) compose, so a cold first call does not
        # look like a hang. warnings dedupes per message, so repeated calls in one
        # process only see this once.
        warnings.warn(
            "plan_trip: auto-composing a timetable for this trip; the first run "
            "downloads flight and transit data (cached afterwards), which can take "
            "a few seconds", stacklevel=3)
        timetable, notes = build_default_timetable(o, d)
        for note in notes:
            warnings.warn(f"plan_trip: {note}", stacklevel=3)
    connectors = _select_connectors(o, d, timetable, access=access, egress=egress,
                                    road=road, turn_aware=turn_aware, region=region,
                                    data_dir=data_dir, connector=connector)
    return o, d, depart_at, timetable, connectors


def plan_trip(origin, dest, depart_at: datetime | None = None,
              timetable: Timetable | None = None,
              *, objective: Objective = Objective.FASTEST, top_n: int = 3,
              conditions: frozenset = frozenset(), geocoder=None,
              road: bool = False, turn_aware: bool = False,
              access: str = "car", egress: str | None = None,
              region: str | None = None, data_dir: str | None = None,
              connector: RoadConnector | None = None,
              exclude_modes: frozenset = frozenset()) -> list[Itinerary]:
    """Rank door-to-door multimodal itineraries between two locations.

    The minimal call is `plan_trip(origin, dest)`: `depart_at` defaults to now and
    `objective` to FASTEST, so two locations are enough to get ranked routes.

    origin/dest accept the same forms as `drive()`: a Location, a (lat, lon)
    tuple, a "lat,lon" string, or a place name (resolved via the active geocoder,
    or a per-call `geocoder=`). `depart_at` is a `datetime` (naive is read as local
    at the origin); omit it to depart now. `timetable` is a GTFS Timetable
    (`load_timetable(feed_dir)` or `sample_timetable()`). If omitted, one is
    auto-composed for the trip: the OpenFlights flight network plus the GTFS
    feed(s) whose coverage area spans the route (Mobility Database catalog),
    fetched and cached on first use. Coverage gaps are reported via warnings;
    pass an explicit `timetable` to control the data exactly.

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
    "both"). `road`/`turn_aware` back a car first/last mile: they apply to "car" and
    to the car arm of "both" (its transit arm stays geometric), but a "transit" or
    asymmetric first/last mile has no car leg to back, so `road` is rejected there
    (raise; build connectors and pass `connector=` for road-backed car legs).

    Note: a "transit" access or egress reaches a stop only within a short walk; if
    no stop is in range, that end has no transit leg and the trip falls back to the
    direct ground (drive/walk) candidate -- so a transit request can still yield a
    car-only itinerary when the door is far from any stop (rather than nothing).

    `exclude_modes` suppresses itineraries using any of the given Modes (e.g.
    `{Mode.FLIGHT}` for a traveller who will not fly) unless that would leave no
    option or only options slower than a same-day rail trip -- see `plan()`. Use it
    with `access="transit"` to express a "trains, not planes" preference.

    Returns up to `top_n` Itinerary objects, best first for `objective`. An EMPTY
    list means no route exists (not an error); an invalid coordinate raises.
    """
    o, d, depart_at, timetable, connectors = _prepare_trip(
        origin, dest, depart_at, timetable, geocoder=geocoder, road=road,
        turn_aware=turn_aware, access=access, egress=egress, region=region,
        data_dir=data_dir, connector=connector)
    return plan_multi(o, d, depart_at, timetable, connectors,
                      conditions=conditions, objective=objective, top_n=top_n,
                      exclude_modes=exclude_modes)


def plan_trip_choices(origin, dest, depart_at: datetime | None = None,
                      timetable: Timetable | None = None, *, objectives,
                      conditions: frozenset = frozenset(), geocoder=None,
                      road: bool = False, turn_aware: bool = False,
                      access: str = "transit", egress: str | None = None,
                      region: str | None = None, data_dir: str | None = None,
                      connector: RoadConnector | None = None,
                      exclude_modes: frozenset = frozenset()) -> list:
    """Door-to-door itineraries as ONE best choice per objective, deduped and
    labelled -- the multi-criteria "choices by purpose" view (e.g. one Fastest, one
    Cheapest, one Greenest card) the demo shows.

    Endpoint, timetable, and connector handling is identical to plan_trip (it
    auto-composes a timetable when none is given, surfacing coverage notes as
    warnings). The difference is the result: instead of ranking by ONE objective it
    returns the leader of each objective in `objectives` -- an ordered sequence of
    (Objective, label) pairs -- with a trip that wins several objectives appearing
    once carrying all its labels. Pair it with a transport preference:
    `access`/`exclude_modes` constrain WHICH modes appear (e.g. transit access with
    flights excluded), while the objectives label HOW each surviving choice is best.

    Returns a list of (Itinerary, list[str]); empty if no route exists.
    """
    o, d, depart_at, timetable, connectors = _prepare_trip(
        origin, dest, depart_at, timetable, geocoder=geocoder, road=road,
        turn_aware=turn_aware, access=access, egress=egress, region=region,
        data_dir=data_dir, connector=connector)
    return plan_labeled(o, d, depart_at, timetable, connectors,
                        objectives=objectives, conditions=conditions,
                        exclude_modes=exclude_modes)
