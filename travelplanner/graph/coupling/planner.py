"""Door-to-door coupling planner with multi-criteria selection (Phases 3-4).

Phases: ground access -> scheduled line-haul (CSA) -> ground egress, plus a
pure-ground candidate. The phase structure enforces legal mode sequences
(no driving in the middle of a transit chain). Output is the v1 Itinerary:
each leg's pre-departure wait is recorded as `overhead`, so total_duration and
arrive_at are schedule-accurate.

Phase 4: candidates are diversified by running the line-haul under different
mode restrictions (all / air-only / surface-only). The Pareto frontier over
(total_duration, cost_rank, transfers) is kept, then ordered by the requested
Objective. AIR_PRIORITY prefers air among non-dominated options (a strictly
dominated flight is dropped, by design). Candidate generation is mode-restricted
diversification, not an exhaustive multi-label search.
"""

from datetime import datetime, timedelta

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

# Line-haul mode restrictions used to diversify candidates.
_MODE_SETS = (
    None,                                   # all modes (earliest arrival)
    frozenset({Mode.FLIGHT}),               # air-only
    frozenset({Mode.TRAIN, Mode.FERRY}),    # surface transit
)


def _stop_location(stop: Stop) -> Location:
    return Location(name=stop.name or stop.id,
                    type=_NODE_TO_LOCATION.get(stop.type, LocationType.STATION),
                    lat=stop.lat, lon=stop.lon)


def _timed_to_legs(timed: list[tuple], depart_at: datetime) -> list[Leg]:
    """Convert (mode, from_loc, to_loc, departure, arrival, distance_km,
    cost_level) tuples into v1 Legs, folding inter-leg waits into overhead."""
    legs: list[Leg] = []
    prev_arrival = depart_at
    for mode, from_loc, to_loc, dep, arr, dist_km, cost in timed:
        legs.append(Leg(
            mode=mode, from_loc=from_loc, to_loc=to_loc,
            distance_km=dist_km,
            travel_time=arr - dep,
            overhead=max(timedelta(), dep - prev_arrival),
            cost_level=cost,
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


def _metrics(itin: Itinerary) -> tuple[float, int, int, float]:
    return (itin.total_duration.total_seconds(), itin.cost_level.rank,
            _transfers(itin), _car_km(itin))


def _dominates(a: Itinerary, b: Itinerary) -> bool:
    ma, mb = _metrics(a), _metrics(b)
    return all(x <= y for x, y in zip(ma, mb)) and any(x < y for x, y in zip(ma, mb))


def _dedupe(cands: list[Itinerary]) -> list[Itinerary]:
    """Drop only TRULY equivalent candidates. The signature covers all four
    ranking axes (time, cost, transfers, car_km) plus the mode sequence, so two
    itineraries that differ on any axis both survive to the Pareto stage -- a
    cheaper or lower-driving option is never collapsed away by an equal-duration
    same-mode sibling that happened to be pooled first."""
    seen: set = set()
    out: list[Itinerary] = []
    for c in cands:
        total, cost, transfers, car_km = _metrics(c)
        sig = (round(total), cost, transfers, round(car_km, 3),
               tuple(leg.mode.value for leg in c.legs))
        if sig in seen:
            continue
        seen.add(sig)
        out.append(c)
    return out


def _pareto(cands: list[Itinerary]) -> list[Itinerary]:
    return [c for c in cands
            if not any(o is not c and _dominates(o, c) for o in cands)]


def _order_key(objective: Objective):
    def key(it: Itinerary):
        total, cost, transfers, car_km = _metrics(it)
        if objective is Objective.FASTEST:
            return (total, cost, transfers)
        if objective is Objective.CHEAPEST:
            return (cost, total, transfers)
        if objective is Objective.FEWEST_TRANSFERS:
            return (transfers, total, cost)
        if objective is Objective.GREENEST:
            # least private-car distance first (keep the transit-preferring
            # intent), then least emissions so a train outranks a flight when
            # both are car-free, then time.
            return (car_km, _emissions(it), total, transfers, cost)
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
        [(leg.mode, origin, dest, depart_at, arr, leg.distance_km, leg.cost_level)],
        depart_at)
    return Itinerary(legs=legs, depart_at=depart_at, score=0.0)


def _transit_itinerary(origin: Location, dest: Location, depart_at: datetime,
                       access: dict[str, AccessLeg], journey: Journey,
                       egress_leg: AccessLeg, egress_stop: str,
                       timetable: Timetable) -> Itinerary:
    board = journey.legs[0].from_stop
    a = access[board]
    timed: list[tuple] = []

    board_loc = _stop_location(timetable.stops[board])
    timed.append((a.mode, origin, board_loc, depart_at,
                  depart_at + timedelta(seconds=a.seconds), a.distance_km,
                  a.cost_level))

    for jl in journey.legs:
        from_loc = _stop_location(timetable.stops[jl.from_stop])
        to_loc = _stop_location(timetable.stops[jl.to_stop])
        dist = haversine(from_loc.lat, from_loc.lon, to_loc.lat, to_loc.lon)
        timed.append((jl.mode, from_loc, to_loc, jl.departure, jl.arrival,
                      dist, jl.cost_level))

    egress_loc = _stop_location(timetable.stops[egress_stop])
    timed.append((egress_leg.mode, egress_loc, dest, journey.arrive,
                  journey.arrive + timedelta(seconds=egress_leg.seconds),
                  egress_leg.distance_km, egress_leg.cost_level))

    return Itinerary(legs=_timed_to_legs(timed, depart_at),
                     depart_at=depart_at, score=0.0)


def _transit_candidate(csa: ConnectionScan, origin: Location, dest: Location,
                       depart_at: datetime, access: dict[str, AccessLeg],
                       sources: dict[str, datetime],
                       egress: dict[str, AccessLeg], timetable: Timetable,
                       conditions: frozenset[str],
                       allowed_modes: frozenset | None) -> Itinerary | None:
    arrivals = csa.arrival_times(sources, conditions, allowed_modes)
    ranked = sorted(
        ((arrivals[sid] + timedelta(seconds=leg.seconds), sid, leg)
         for sid, leg in egress.items() if sid in arrivals),
        key=lambda x: x[0])
    for _, e_stop, e_leg in ranked:
        journey = csa.query(sources, e_stop, conditions, allowed_modes)
        if journey is not None:
            return _transit_itinerary(origin, dest, depart_at, access, journey,
                                      e_leg, e_stop, timetable)
    return None


def _candidates(origin: Location, dest: Location, depart_at: datetime,
                timetable: Timetable, connector: RoadConnector,
                conditions: frozenset[str], horizon: timedelta) -> list[Itinerary]:
    """All door-to-door candidates a single connector yields (pre-Pareto): the
    pure-ground option plus one transit option per line-haul mode restriction."""
    day = depart_at.date()
    candidates: list[Itinerary] = []

    ground = connector.direct(origin, dest, conditions, day=day)
    if ground is not None:
        candidates.append(_ground_itinerary(origin, dest, depart_at, ground))

    access = connector.access(origin, conditions, day=day)
    sources = {sid: depart_at + timedelta(seconds=leg.seconds)
               for sid, leg in access.items() if sid in timetable.stops}
    egress = {sid: leg for sid, leg in
              connector.egress(dest, conditions, day=day).items()
              if sid in timetable.stops}
    if sources and egress:
        csa = ConnectionScan(timetable, horizon)
        for allowed in _MODE_SETS:
            itin = _transit_candidate(csa, origin, dest, depart_at, access,
                                      sources, egress, timetable, conditions,
                                      allowed)
            if itin is not None:
                candidates.append(itin)
    return candidates


def _rank(candidates: list[Itinerary], objective: Objective,
          top_n: int) -> list[Itinerary]:
    """Pareto-filter, score, and order candidates for the objective; keep top_n."""
    frontier = _pareto(_dedupe(candidates))
    for itin in frontier:
        itin.score = itin.total_duration.total_seconds()
    frontier.sort(key=_order_key(objective))
    return frontier[:top_n]


def plan(origin: Location, dest: Location, depart_at: datetime,
         timetable: Timetable, connector: RoadConnector, *,
         conditions: frozenset[str] = frozenset(),
         objective: Objective = Objective.AIR_PRIORITY,
         top_n: int = 3,
         horizon: timedelta = timedelta(days=2)) -> list[Itinerary]:
    """Rank Pareto-optimal door-to-door itineraries for the given objective.

    Returns a list of up to top_n Itinerary objects, best first. An EMPTY list
    means no route exists for the date/conditions (e.g. an out-of-season ferry
    with no road alternative) -- it is not an error. Invalid input (e.g. an
    out-of-range coordinate) raises instead, so empty != bad input.

    Each Itinerary exposes: legs (list[Leg]), depart_at / arrive_at (datetime,
    naive local time), total_duration (timedelta; total_minutes for a float),
    total_distance_km, primary_mode (Mode of the longest leg), cost_level
    (CostLevel, the max over legs), and num_transfers (line-haul changes). Each
    Leg has mode, from_loc/to_loc, distance_km, travel_time and overhead (wait)
    timedeltas, and cost_level. Use to_dict()/to_json() or itinerary_records /
    leg_records for JSON or tabular output.
    """
    return _rank(_candidates(origin, dest, depart_at, timetable, connector,
                             conditions, horizon), objective, top_n)


def plan_multi(origin: Location, dest: Location, depart_at: datetime,
               timetable: Timetable, connectors, *,
               conditions: frozenset[str] = frozenset(),
               objective: Objective = Objective.AIR_PRIORITY,
               top_n: int = 3,
               horizon: timedelta = timedelta(days=2)) -> list[Itinerary]:
    """Like plan(), but pools candidates from SEVERAL connectors before the
    single Pareto/ranking pass. Use it to diversify the first/last mile -- e.g. a
    car-access and a transit-access connector -- so a drive-to-airport itinerary
    and a walk-to-train one compete on one frontier (the latter would otherwise
    never be generated). Same return contract as plan()."""
    pooled: list[Itinerary] = []
    for connector in connectors:
        pooled += _candidates(origin, dest, depart_at, timetable, connector,
                              conditions, horizon)
    return _rank(pooled, objective, top_n)
