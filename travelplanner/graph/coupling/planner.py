"""Door-to-door coupling planner with multi-criteria selection (Phases 3-4).

Phases: ground access -> scheduled line-haul (CSA) -> ground egress, plus a
pure-ground candidate. The phase structure enforces legal mode sequences
(no driving in the middle of a transit chain). Output is the v1 Itinerary:
each leg's pre-departure wait is recorded as `overhead`, so total_duration and
arrive_at are schedule-accurate.

Phase 4: candidates are diversified by running the line-haul under different
mode restrictions (all / air-only / surface-only). The Pareto frontier is kept
over the objective's own axes -- (total_duration, cost_rank, transfers), and for
GREENEST and AIR_PRIORITY also private-car distance and emissions -- then ordered
by the requested Objective. So a greener-but-slower option is kept for GREENEST,
and a slower-but-car-free flight is kept for AIR_PRIORITY, but neither is padded
into a FASTEST/CHEAPEST result. AIR_PRIORITY prefers air among non-dominated
options (a flight dominated on every axis including car_km/emissions is dropped).
Candidate generation is mode-restricted diversification, not an exhaustive
multi-label search.
"""

from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from travelplanner.fares import DEFAULT_CURRENCY, get_fare_model
from travelplanner.geo import haversine
from travelplanner.models import Itinerary, Leg, Location, LocationType, Mode
from travelplanner.graph.coupling.connector import AccessLeg, RoadConnector
from travelplanner.graph.query import Objective
from travelplanner.graph.scheduled.csa import ConnectionScan, Journey
from travelplanner.graph.scheduled.model import Stop, Timetable
from travelplanner.graph.schema import NodeType

_NODE_TO_LOCATION = {
    NodeType.AIRPORT: LocationType.AIRPORT,
    NodeType.RAIL_STATION: LocationType.STATION,
    NodeType.FERRY_TERMINAL: LocationType.STATION,
}
_VEHICLE_MODES = frozenset({Mode.TRAIN, Mode.FERRY, Mode.FLIGHT})

# A walk-only (footpath-chain) journey beyond this is not a realistic transit
# option (footpaths are transitively closed with no cap, so a mis-imported chain
# could otherwise surface as a 100+ km "walk"). Shorter footpath routes are kept.
_MAX_WALK_ONLY_KM = 10.0

# Implausible-transit guard. When a corridor's feed lacks the real through-train, the
# scan can stitch a gross detour or a many-hop chain out of sparse stops; such a journey
# is dropped rather than shown. Two detour shapes, two tests:
#  - a transfer-detour (Vienna->Venice routed via Stuttgart) -- each leg rides at normal
#    speed, but the leg distances sum to far more than the trip: caught by the ratio.
#  - a single run looping via a far intermediate stop -- it collapses to one leg with
#    near endpoints but a long RIDE time: caught by the per-leg speed floor (ride time,
#    not total, so a legitimate wait for a scheduled service is not flagged).
# Plus a leg-count cap for stitched many-hop chains. The distance gate avoids distorting
# a tiny great-circle. Below the gate, transit is left alone (short trips are local).
_PLAUSIBLE_MIN_KM = 75.0
_MIN_LEG_KMH = 25.0
_MAX_DETOUR_RATIO = 2.0
_MAX_JOURNEY_LEGS = 10

# Absolute ride-time sanity at ANY distance (the per-leg speed and detour checks above
# only apply past _PLAUSIBLE_MIN_KM, so a short trip is otherwise unguarded): the time
# actually spent moving must not exceed a generous floor for the straight-line trip,
# or a short hop stitched into an hours-long chain (a 35 km trip returned as a 15 h
# ride) slips through. Generous -- a base allowance plus a low overall km/h -- so a
# real regional or long ride is kept; it counts ride time only (not waits), so a
# sparse-but-legitimate schedule with a long wait is not flagged.
_RIDE_TIME_BASE_H = 3.0
_MIN_OVERALL_RIDE_KMH = 15.0

# A preferred-mode exclusion (a traveller who will not fly) suppresses candidates
# using the excluded mode -- but only while a same-day alternative survives. If the
# best remaining option is slower than this, the excluded mode is the only realistic
# way there within a day, so the exclusion is bypassed and the flight is shown again.
# This is what makes "avoid flying" hide flights on a rail-doable corridor yet still
# surface the flight when the only ground route is an overnight, multi-day slog.
_EXCLUDE_FALLBACK_HOURS = 16.0

# Line-haul mode restrictions used to diversify candidates.
_MODE_SETS = (
    None,                                   # all modes (earliest arrival)
    frozenset({Mode.FLIGHT}),               # air-only
    frozenset({Mode.TRAIN, Mode.FERRY}),    # surface transit
)

# Mode restrictions for the fewest-changes pass that surfaces a direct or low-change
# through-service for MOST_DIRECT. Air-only is omitted: the earliest-arrival air
# candidate is already the most direct flight when one exists, so an air pass would
# only duplicate it. All-modes covers a direct flight; surface-transit a direct train.
_DIRECT_MODE_SETS = (
    None,
    frozenset({Mode.TRAIN, Mode.FERRY}),
)


def _stop_location(stop: Stop) -> Location:
    return Location(name=stop.name or stop.id,
                    type=_NODE_TO_LOCATION.get(stop.type, LocationType.STATION),
                    lat=stop.lat, lon=stop.lon, tz=stop.tz)


def _timed_to_legs(timed: list[tuple], depart_at: datetime) -> list[Leg]:
    """Convert (mode, from_loc, to_loc, departure, arrival, distance_km,
    cost_level, geometry) tuples into v1 Legs, folding inter-leg waits into
    overhead. geometry is the routed polyline for a road leg, else None."""
    legs: list[Leg] = []
    prev_arrival = depart_at
    fare_model = get_fare_model()
    currency = getattr(fare_model, "currency", DEFAULT_CURRENCY)
    for mode, from_loc, to_loc, dep, arr, dist_km, cost, geometry in timed:
        legs.append(Leg(
            mode=mode, from_loc=from_loc, to_loc=to_loc,
            distance_km=dist_km,
            travel_time=arr - dep,
            overhead=max(timedelta(), dep - prev_arrival),
            cost_level=cost,
            geometry=geometry,
            fare_estimate=round(fare_model(mode, dist_km), 2),
            fare_currency=currency,
        ))
        prev_arrival = arr
    return legs


def _transfers(itin: Itinerary) -> int:
    vehicles = sum(1 for leg in itin.legs if leg.mode in _VEHICLE_MODES)
    return max(0, vehicles - 1)


def _car_km(itin: Itinerary) -> float:
    """Private-car distance: the axis a traveler who prefers transit minimizes."""
    return sum(leg.distance_km for leg in itin.legs if leg.mode is Mode.CAR)


# Rough per-passenger emissions (g CO2 / km) by mode for the GREENEST ranking: a
# flight is the worst per km, rail the best motorised option, walking free. These
# rank options sensibly (flight >> car > ferry > train > walk); they are not a
# precise carbon model.
_EMISSIONS_G_PER_KM = {
    Mode.WALK: 0.0, Mode.TRAIN: 35.0, Mode.FERRY: 120.0,
    Mode.CAR: 170.0, Mode.FLIGHT: 250.0,
}


def _emissions(itin: Itinerary) -> float:
    """Approximate trip CO2 (g): per-mode factor times each leg's distance.

    Used to order GREENEST so a flight never outranks a train -- minimizing
    private-car distance alone treated a flight and a train as equally green.
    """
    return sum(leg.distance_km * _EMISSIONS_G_PER_KM.get(leg.mode, 120.0)
               for leg in itin.legs)


def _fare(itin: Itinerary) -> float:
    """Approximate total fare (active model's currency) used as the cost axis: the
    sum of leg estimates. Falls back to the 3-level cost_level band when an
    itinerary is unpriced, so a candidate is never dropped for lacking an estimate
    and ranking still works if the fare model is disabled."""
    fare = itin.fare_estimate
    return fare if fare is not None else float(itin.cost_level.rank)


def _metrics(itin: Itinerary) -> tuple[float, float, int, float, float]:
    # (time, cost, transfers, car_km, emissions). cost is the approximate fare (a
    # continuous amount, so CHEAPEST separates same-band options instead of tying on
    # the 3-level band). car_km and emissions are ranking dimensions only for
    # GREENEST, so they are frontier axes only for GREENEST (see _objective_axes):
    # keeping them for every objective would let a car-free/low-emission option
    # survive the frontier and surface a strictly slower-and-pricier trip under
    # FASTEST/CHEAPEST.
    return (itin.total_duration.total_seconds(), _fare(itin),
            _transfers(itin), _car_km(itin), _emissions(itin))


def _tuple_dominates(a: tuple, b: tuple) -> bool:
    return all(x <= y for x, y in zip(a, b)) and any(x < y for x, y in zip(a, b))


# Objectives whose preference is not captured by (time, cost, transfers) alone, so
# they keep the full frontier: GREENEST ranks car_km/emissions; AIR_PRIORITY wants
# a flight, which is non-dominated only via the car_km/emissions axes (there is no
# "flies" axis); MOST_DIRECT wants the car-free direct ride, which a faster drive
# would dominate on the 3 core axes (it has no transfers either) -- the car_km axis
# is what keeps it on the frontier. Restricting these to the 3 core axes would prune
# the very option they prefer (the greener option, the car-free flight, the train).
_FULL_FRONTIER_OBJECTIVES = frozenset(
    {Objective.GREENEST, Objective.AIR_PRIORITY, Objective.MOST_DIRECT})


def _objective_axes(itin: Itinerary, objective: Objective) -> tuple:
    """Pareto axes for an objective: the full (time, cost, transfers, car_km,
    emissions) for GREENEST/AIR_PRIORITY, else only (time, cost, transfers) so the
    greener axes do not keep an option that is strictly worse on the requested
    ones. Low-car diversification is therefore surfaced under GREENEST (where it
    ranks), not padded into a FASTEST/CHEAPEST result."""
    m = _metrics(itin)
    return m if objective in _FULL_FRONTIER_OBJECTIVES else m[:3]


def _signature(itin: Itinerary) -> tuple:
    """Ranking identity of an itinerary: all five axes (time, cost, transfers,
    car_km, emissions) plus its mode sequence. Two itineraries with the same
    signature are interchangeable for ranking -- used both to dedupe candidates and
    to recognise that a leader winning several objectives is one choice."""
    total, cost, transfers, car_km, emissions = _metrics(itin)
    return (round(total), round(cost, 2), transfers, round(car_km, 3),
            round(emissions, 1), tuple(leg.mode.value for leg in itin.legs))


def _dedupe(cands: list[Itinerary]) -> list[Itinerary]:
    """Drop only TRULY equivalent candidates. The signature covers all five
    ranking axes (time, cost, transfers, car_km, emissions) plus the mode
    sequence, so two itineraries that differ on any axis both survive to the
    Pareto stage -- a cheaper, lower-driving or greener option is never collapsed
    away by an equal-duration same-mode sibling that happened to be pooled first."""
    seen: set = set()
    out: list[Itinerary] = []
    for c in cands:
        sig = _signature(c)
        if sig in seen:
            continue
        seen.add(sig)
        out.append(c)
    return out


def _frontier(cands: list[Itinerary], objective: Objective) -> list[Itinerary]:
    """Pareto frontier on the objective's own axes (see _objective_axes)."""
    axes = [(_objective_axes(c, objective), c) for c in cands]
    return [c for ma, c in axes
            if not any(o is not c and _tuple_dominates(mb, ma) for mb, o in axes)]


def _order_key(objective: Objective):
    def key(it: Itinerary):
        total, cost, transfers, car_km, emissions = _metrics(it)
        if objective is Objective.FASTEST:
            return (total, cost, transfers)
        if objective is Objective.CHEAPEST:
            return (cost, total, transfers)
        if objective is Objective.FEWEST_TRANSFERS:
            return (transfers, total, cost)
        if objective is Objective.MOST_DIRECT:
            # Fewest scheduled vehicle legs, e.g. one through-train over a
            # change-at-the-border chain. A pure drive has no scheduled leg, so it
            # sorts last (leading 1) -- "most direct" means the most direct ride, not
            # "skip transit and drive". Then fewest legs, then fastest, then cheapest.
            vehicles = sum(1 for leg in it.legs if leg.mode in _VEHICLE_MODES)
            return (0 if vehicles else 1, vehicles, total, cost)
        if objective is Objective.GREENEST:
            # least modelled emissions first, then least private-car distance, then
            # time. Emissions (not car-km) leads so a short-access flight never reads
            # as greener than driving or rail -- minimising driving alone ranked a
            # 230 km flight above the drive because the flight barely touches a car.
            # car_km/emissions are GREENEST frontier axes (see _objective_axes) so the
            # greener option survives the filter.
            return (emissions, car_km, total, transfers, cost)
        # AIR_PRIORITY: prefer an itinerary that actually flies. Test for a FLIGHT
        # leg, not primary_mode (the longest leg) -- a long airport-access drive
        # could otherwise make a genuine flight rank as non-air.
        air = 0 if any(leg.mode is Mode.FLIGHT for leg in it.legs) else 1
        return (air, total, cost, transfers)
    return key


def _ground_itinerary(origin: Location, dest: Location, depart_at: datetime,
                      leg: AccessLeg) -> Itinerary:
    arr = depart_at + timedelta(seconds=leg.seconds)
    legs = _timed_to_legs(
        [(leg.mode, origin, dest, depart_at, arr, leg.distance_km, leg.cost_level,
          leg.geometry)],
        depart_at)
    return Itinerary(legs=legs, depart_at=depart_at, score=0.0)


def _transit_itinerary(origin: Location, dest: Location, depart_at: datetime,
                       access: dict[str, AccessLeg], journey: Journey,
                       egress_leg: AccessLeg, egress_stop: str,
                       timetable: Timetable) -> Itinerary:
    board = journey.legs[0].from_stop
    a = access[board]
    timed: list[tuple] = []

    # Give the door endpoints the timezone of their adjacent stop, so the first
    # and last legs render in the traveler's local time at each end.
    board_loc = _stop_location(timetable.stops[board])
    egress_stop_obj = timetable.stops[egress_stop]
    origin = replace(origin, tz=origin.tz or board_loc.tz)
    dest = replace(dest, tz=dest.tz or egress_stop_obj.tz)

    timed.append((a.mode, origin, board_loc, depart_at,
                  depart_at + timedelta(seconds=a.seconds), a.distance_km,
                  a.cost_level, a.geometry))

    for jl in journey.legs:
        from_loc = _stop_location(timetable.stops[jl.from_stop])
        to_loc = _stop_location(timetable.stops[jl.to_stop])
        dist = haversine(from_loc.lat, from_loc.lon, to_loc.lat, to_loc.lon)
        timed.append((jl.mode, from_loc, to_loc, jl.departure, jl.arrival,
                      dist, jl.cost_level, None))   # transit leg: no road polyline

    egress_loc = _stop_location(timetable.stops[egress_stop])
    timed.append((egress_leg.mode, egress_loc, dest, journey.arrive,
                  journey.arrive + timedelta(seconds=egress_leg.seconds),
                  egress_leg.distance_km, egress_leg.cost_level,
                  egress_leg.geometry))

    return Itinerary(legs=_timed_to_legs(timed, depart_at),
                     depart_at=depart_at, score=0.0)


def _implausible_transit(itin: Itinerary) -> bool:
    """True for a transit itinerary no traveller would take (see the guard constants):
    too many legs, a ride far longer than the straight-line trip warrants, a single
    vehicle leg that rode far around for its endpoints, or a journey whose leg
    distances sum to a gross multiple of the straight-line trip."""
    if len(itin.legs) > _MAX_JOURNEY_LEGS:
        return True
    trip = haversine(itin.legs[0].from_loc.lat, itin.legs[0].from_loc.lon,
                     itin.legs[-1].to_loc.lat, itin.legs[-1].to_loc.lon)
    # Absolute ride-time sanity at any distance: a short hop stitched into an
    # hours-long ride (a 35 km trip returned as 15 h) is dropped even below the
    # distance gate the speed/detour checks use.
    ride_hours = sum(leg.travel_time.total_seconds()
                     for leg in itin.legs) / 3600.0
    if ride_hours > _RIDE_TIME_BASE_H + trip / _MIN_OVERALL_RIDE_KMH:
        return True
    leg_km = 0.0
    for leg in itin.legs:
        gc = haversine(leg.from_loc.lat, leg.from_loc.lon,
                       leg.to_loc.lat, leg.to_loc.lon)
        leg_km += gc
        if leg.mode is Mode.WALK:
            continue
        hours = leg.travel_time.total_seconds() / 3600.0
        if gc > _PLAUSIBLE_MIN_KM and hours > 0 and gc / hours < _MIN_LEG_KMH:
            return True                       # a single leg looping via a far stop
    return trip > _PLAUSIBLE_MIN_KM and leg_km / trip > _MAX_DETOUR_RATIO


def _itinerary_from_journey(origin: Location, dest: Location, depart_at: datetime,
                            access: dict[str, AccessLeg], journey,
                            e_leg: AccessLeg, e_stop: str,
                            timetable: Timetable) -> Itinerary | None:
    """Build a door-to-door Itinerary from an in-network journey plus the chosen
    egress, or None if the journey is unusable -- a dangling (unlocated) stop, an
    over-long walk-only route, or an implausible detour / many-hop chain."""
    if journey is None:
        return None
    # Skip a journey riding through a stop with no Stop entry (a dangling trip/footpath
    # reference): it cannot be located. CSA already skips interior stops, so a feed
    # with an unregistered interior stop still plans -- only a journey actually
    # touching the dangling stop is routed around. (Runs first so the coord lookups
    # below are safe.)
    if any(leg.from_stop not in timetable.stops
           or leg.to_stop not in timetable.stops for leg in journey.legs):
        return None
    # A walk-only journey (no vehicle leg) bypasses the mode restriction; keep it only
    # if it is a reasonable walk, not an over-long footpath chain (driving is already
    # covered by the direct ground candidate).
    if not any(leg.mode is not Mode.WALK for leg in journey.legs):
        walk_km = sum(
            haversine(timetable.stops[leg.from_stop].lat,
                      timetable.stops[leg.from_stop].lon,
                      timetable.stops[leg.to_stop].lat,
                      timetable.stops[leg.to_stop].lon)
            for leg in journey.legs)
        if walk_km > _MAX_WALK_ONLY_KM:
            return None
    itin = _transit_itinerary(origin, dest, depart_at, access, journey,
                              e_leg, e_stop, timetable)
    # Drop an implausible journey (a gross detour, or a many-hop chain stitched from
    # sparse stops when the real through-service is missing).
    if _implausible_transit(itin):
        return None
    return itin


def _transit_candidate(csa: ConnectionScan, origin: Location, dest: Location,
                       depart_at: datetime, access: dict[str, AccessLeg],
                       sources: dict[str, datetime],
                       egress: dict[str, AccessLeg], timetable: Timetable,
                       conditions: frozenset[str],
                       allowed_modes: frozenset | None) -> Itinerary | None:
    """The earliest-arriving transit itinerary for a mode set: rank egress stops by
    door arrival, take the first that yields a plausible journey."""
    arrivals = csa.arrival_times(sources, conditions, allowed_modes)
    ranked = sorted(
        ((arrivals[sid] + timedelta(seconds=leg.seconds), sid, leg)
         for sid, leg in egress.items() if sid in arrivals),
        key=lambda x: x[0])
    for _, e_stop, e_leg in ranked:
        itin = _itinerary_from_journey(
            origin, dest, depart_at, access,
            csa.query(sources, e_stop, conditions, allowed_modes),
            e_leg, e_stop, timetable)
        if itin is not None:
            return itin
    return None


def _min_transfer_candidate(csa: ConnectionScan, origin: Location, dest: Location,
                            depart_at: datetime, access: dict[str, AccessLeg],
                            sources: dict[str, datetime],
                            egress: dict[str, AccessLeg], timetable: Timetable,
                            conditions: frozenset[str],
                            allowed_modes: frozenset | None) -> Itinerary | None:
    """The fewest-changes transit itinerary for a mode set: rank egress by changes
    first, then door arrival. Surfaces a direct or low-change ride (e.g. a through-IC
    plus a short feeder) that the earliest-arrival scan skips for a faster, more-hop
    route -- the candidate MOST_DIRECT ranks first."""
    arrivals = csa.min_transfer_arrivals(sources, conditions, allowed_modes)
    ranked = sorted(
        ((arrivals[sid][0], arrivals[sid][1] + timedelta(seconds=leg.seconds),
          sid, leg) for sid, leg in egress.items() if sid in arrivals),
        key=lambda x: (x[0], x[1]))
    for _trips, _door, e_stop, e_leg in ranked:
        itin = _itinerary_from_journey(
            origin, dest, depart_at, access,
            csa.min_transfer_query(sources, e_stop, conditions, allowed_modes),
            e_leg, e_stop, timetable)
        if itin is not None:
            return itin
    return None


def _normalize_depart(origin: Location, depart_at: datetime,
                      timetable: Timetable) -> datetime:
    """Read a naive departure as local at the origin for a tz-aware feed.

    The output itinerary's only stored absolute time is depart_at (legs carry
    durations), so making it aware in the origin's zone keeps a single-timezone
    trip's displayed clock identical to before while letting the scan reconcile
    it against UTC-materialized connections. A naive depart_at over a feed with no
    timezone data, or an already-aware depart_at, is left untouched.
    """
    if not timetable.tz_aware():
        # Naive feed: keep the whole pipeline naive (the access/egress legs and
        # the connections are naive), so an aware depart_at must shed its tzinfo
        # or it would crash comparing against naive connection times.
        return depart_at.replace(tzinfo=None) if depart_at.tzinfo else depart_at
    if depart_at.tzinfo is not None:
        return depart_at
    name = timetable.zone_for_point(origin.lat, origin.lon)
    return depart_at.replace(tzinfo=ZoneInfo(name)) if name else depart_at


def _candidates(origin: Location, dest: Location, depart_at: datetime,
                timetable: Timetable, connector: RoadConnector,
                conditions: frozenset[str], horizon: timedelta) -> list[Itinerary]:
    """All door-to-door candidates a single connector yields (pre-Pareto): the
    pure-ground option plus one transit option per line-haul mode restriction."""
    depart_at = _normalize_depart(origin, depart_at, timetable)
    day = depart_at.date()
    candidates: list[Itinerary] = []

    ground = connector.direct(origin, dest, conditions, day=day,
                              depart_at=depart_at)
    if ground is not None:
        candidates.append(_ground_itinerary(origin, dest, depart_at, ground))

    access = connector.access(origin, conditions, day=day, depart_at=depart_at)
    sources = {sid: depart_at + timedelta(seconds=leg.seconds)
               for sid, leg in access.items() if sid in timetable.stops}
    # Egress legs are priced for every candidate stop up front, before the journey
    # (and thus the actual arrival time) is known, so their time-of-day congestion
    # is referenced to depart_at like access/direct. Exact for same-period trips;
    # an approximation for a long/overnight trip where arrival sits in a different
    # congestion band -- still far better than ignoring time of day entirely.
    egress = {sid: leg for sid, leg in
              connector.egress(dest, conditions, day=day,
                               depart_at=depart_at).items()
              if sid in timetable.stops}
    if sources and egress:
        csa = ConnectionScan(timetable, horizon)
        for allowed in _MODE_SETS:
            itin = _transit_candidate(csa, origin, dest, depart_at, access,
                                      sources, egress, timetable, conditions,
                                      allowed)
            if itin is not None:
                candidates.append(itin)
        # Also offer the fewest-changes ride per mode set. The earliest-arrival scan
        # above takes a faster, more-hop route over a slower direct/low-change one, so
        # a through-train is never produced; this adds it to the pool for MOST_DIRECT
        # to rank (a duplicate of an above candidate is dropped by the Pareto dedupe).
        for allowed in _DIRECT_MODE_SETS:
            itin = _min_transfer_candidate(csa, origin, dest, depart_at, access,
                                           sources, egress, timetable, conditions,
                                           allowed)
            if itin is not None:
                candidates.append(itin)
    return candidates


def _apply_exclusions(candidates: list[Itinerary],
                      exclude_modes: frozenset) -> list[Itinerary]:
    """Drop candidates that use an excluded mode (a traveller's "do not fly"), unless
    that leaves nothing or only options slower than a same-day rail trip
    (_EXCLUDE_FALLBACK_HOURS) -- then the excluded mode is the only realistic way
    there and every candidate is kept. Returns the input unchanged when nothing is
    excluded, so it is a no-op for the default (empty) exclusion set."""
    if not exclude_modes:
        return candidates
    kept = [c for c in candidates
            if not any(leg.mode in exclude_modes for leg in c.legs)]
    if not kept:
        return candidates
    best = min(c.total_duration.total_seconds() for c in kept)
    if best > _EXCLUDE_FALLBACK_HOURS * 3600.0:
        return candidates
    return kept


def _rank(candidates: list[Itinerary], objective: Objective,
          top_n: int) -> list[Itinerary]:
    """Pareto-filter, score, and order candidates for the objective; keep top_n."""
    frontier = _frontier(_dedupe(candidates), objective)
    for itin in frontier:
        itin.score = itin.total_duration.total_seconds()
    frontier.sort(key=_order_key(objective))
    return frontier[:top_n]


def plan(origin: Location, dest: Location, depart_at: datetime,
         timetable: Timetable, connector: RoadConnector, *,
         conditions: frozenset[str] = frozenset(),
         objective: Objective = Objective.FASTEST,
         top_n: int = 3,
         horizon: timedelta = timedelta(days=2),
         exclude_modes: frozenset = frozenset()) -> list[Itinerary]:
    """Rank Pareto-optimal door-to-door itineraries for the given objective.

    Returns a list of up to top_n Itinerary objects, best first. An EMPTY list
    means no route exists for the date/conditions (e.g. an out-of-season ferry
    with no road alternative) -- it is not an error. Invalid input (e.g. an
    out-of-range coordinate) raises instead, so empty != bad input. A journey that
    would ride through a dangling (unregistered) stop is routed around, not crashed.

    `exclude_modes` suppresses itineraries that use any of the given Modes (e.g.
    `{Mode.FLIGHT}` for a traveller who will not fly) before ranking -- unless that
    would leave no option, or only options slower than a same-day rail trip, in
    which case the excluded mode is the only realistic way there and the candidates
    are kept. Default (empty) excludes nothing.

    Each Itinerary exposes: legs (list[Leg]), depart_at / arrive_at (datetime,
    naive local time), total_duration (timedelta; total_minutes for a float),
    total_distance_km, primary_mode (Mode of the longest leg), cost_level
    (CostLevel, the max over legs), and num_transfers (line-haul changes). Each
    Leg has mode, from_loc/to_loc, distance_km, travel_time and overhead (wait)
    timedeltas, and cost_level. Use to_dict()/to_json() or itinerary_records /
    leg_records for JSON or tabular output.
    """
    candidates = _candidates(origin, dest, depart_at, timetable, connector,
                             conditions, horizon)
    return _rank(_apply_exclusions(candidates, exclude_modes), objective, top_n)


def plan_multi(origin: Location, dest: Location, depart_at: datetime,
               timetable: Timetable, connectors, *,
               conditions: frozenset[str] = frozenset(),
               objective: Objective = Objective.FASTEST,
               top_n: int = 3,
               horizon: timedelta = timedelta(days=2),
               exclude_modes: frozenset = frozenset()) -> list[Itinerary]:
    """Like plan(), but pools candidates from SEVERAL connectors before the
    single Pareto/ranking pass. Use it to diversify the first/last mile -- e.g. a
    car-access and a transit-access connector -- so a drive-to-airport itinerary
    and a walk-to-train one compete on one frontier (the latter would otherwise
    never be generated). `exclude_modes` is applied to the pooled candidates as in
    plan(). Same return contract as plan()."""
    pooled: list[Itinerary] = []
    for connector in connectors:
        pooled += _candidates(origin, dest, depart_at, timetable, connector,
                              conditions, horizon)
    return _rank(_apply_exclusions(pooled, exclude_modes), objective, top_n)


def plan_labeled(origin: Location, dest: Location, depart_at: datetime,
                 timetable: Timetable, connectors, *,
                 objectives, conditions: frozenset[str] = frozenset(),
                 exclude_modes: frozenset = frozenset(),
                 horizon: timedelta = timedelta(days=2)) -> list:
    """The single best itinerary for EACH objective, deduped -- the "choices
    labelled by purpose" view (e.g. one Fastest, one Cheapest, one Greenest card).

    `objectives` is an ordered sequence of (Objective, label) pairs. A trip that
    wins several objectives appears once, carrying all the labels it won, under the
    earliest objective it wins (so the result order follows `objectives`).
    Candidates are pooled from the connectors ONCE and then ranked per objective
    (cheap), so this costs about one plan_multi regardless of how many objectives
    are requested. `exclude_modes` is applied to the pool as in plan().

    Returns a list of (Itinerary, list[str]); empty when no route exists.
    """
    pooled: list[Itinerary] = []
    for connector in connectors:
        pooled += _candidates(origin, dest, depart_at, timetable, connector,
                              conditions, horizon)
    pooled = _apply_exclusions(pooled, exclude_modes)
    chosen: list[list] = []          # [itinerary, [labels]] in objective order
    at: dict[tuple, int] = {}        # signature -> index in chosen
    for objective, label in objectives:
        top = _rank(pooled, objective, 1)
        if not top:
            continue
        leader = top[0]
        sig = _signature(leader)
        if sig in at:
            labels = chosen[at[sig]][1]
            if label not in labels:
                labels.append(label)
        else:
            at[sig] = len(chosen)
            chosen.append([leader, [label]])
    return [(itin, labels) for itin, labels in chosen]
