"""Itinerary generation, scoring, and ranking."""

from datetime import datetime, timedelta

from travelplanner.catalog import airport_to_location, nearest_airport
from travelplanner.geo import haversine, road_distance
from travelplanner.models import Itinerary, Leg, Location, LocationType, Mode
from travelplanner.modes import PlannerConfig


def _build_leg(mode: Mode, from_loc: Location, to_loc: Location,
               distance_km: float, config: PlannerConfig) -> Leg:
    profile = config.profiles[mode]
    hours = distance_km / profile.avg_speed_kmh if profile.avg_speed_kmh else 0.0
    return Leg(
        mode=mode,
        from_loc=from_loc,
        to_loc=to_loc,
        distance_km=distance_km,
        travel_time=timedelta(hours=hours),
        overhead=profile.overhead,
        cost_level=profile.cost_level,
    )


def _ground_candidates(origin: Location, dest: Location,
                       config: PlannerConfig) -> list[list[Leg]]:
    road_km = road_distance(origin.lat, origin.lon, dest.lat, dest.lon,
                            config.detour_factor)
    candidates: list[list[Leg]] = []

    walk = config.profiles[Mode.WALK]
    if road_km <= walk.max_distance_km:
        candidates.append([_build_leg(Mode.WALK, origin, dest, road_km, config)])

    candidates.append([_build_leg(Mode.CAR, origin, dest, road_km, config)])

    train = config.profiles[Mode.TRAIN]
    if road_km >= train.min_distance_km:
        candidates.append([_build_leg(Mode.TRAIN, origin, dest, road_km, config)])

    return candidates


def _air_candidate(origin: Location, dest: Location,
                   config: PlannerConfig) -> list[Leg] | None:
    air_km = haversine(origin.lat, origin.lon, dest.lat, dest.lon)
    if air_km < config.min_air_distance_km:
        return None

    if origin.type is LocationType.AIRPORT:
        dep = origin
    else:
        dep = airport_to_location(nearest_airport(origin.lat, origin.lon))
    if dest.type is LocationType.AIRPORT:
        arr = dest
    else:
        arr = airport_to_location(nearest_airport(dest.lat, dest.lon))

    flight_km = haversine(dep.lat, dep.lon, arr.lat, arr.lon)
    if flight_km < config.profiles[Mode.FLIGHT].min_distance_km:
        return None

    legs: list[Leg] = []
    access_km = road_distance(origin.lat, origin.lon, dep.lat, dep.lon,
                              config.detour_factor)
    if origin.type is not LocationType.AIRPORT and access_km > 0.1:
        legs.append(_build_leg(Mode.CAR, origin, dep, access_km, config))

    legs.append(_build_leg(Mode.FLIGHT, dep, arr, flight_km, config))

    egress_km = road_distance(arr.lat, arr.lon, dest.lat, dest.lon,
                              config.detour_factor)
    if dest.type is not LocationType.AIRPORT and egress_km > 0.1:
        legs.append(_build_leg(Mode.CAR, arr, dest, egress_km, config))

    return legs


def _score(legs: list[Leg], config: PlannerConfig) -> float:
    total_hours = sum((leg.duration for leg in legs),
                      timedelta()).total_seconds() / 3600.0
    cost_rank_sum = sum(leg.cost_level.rank for leg in legs)
    primary_mode = max(legs, key=lambda leg: leg.distance_km).mode
    score = config.w_time * total_hours + config.w_cost * cost_rank_sum
    if primary_mode is Mode.FLIGHT:
        score -= config.air_bonus
    return score


def plan(origin: Location, dest: Location, start: datetime,
         end: datetime | None = None, *,
         config: PlannerConfig | None = None) -> list[Itinerary]:
    """Rank door-to-door itineraries between two locations.

    start: earliest departure time.
    end: optional latest arrival time; itineraries arriving later are marked
         infeasible and sorted after feasible ones.
    """
    config = config or PlannerConfig()

    leg_sets = list(_ground_candidates(origin, dest, config))
    air = _air_candidate(origin, dest, config)
    if air is not None:
        leg_sets.append(air)

    itineraries: list[Itinerary] = []
    for legs in leg_sets:
        itin = Itinerary(legs=legs, depart_at=start, score=_score(legs, config),
                         arrival_window_end=end)
        if end is not None:
            itin.feasible = itin.arrive_at <= end
            itin.slack = end - itin.arrive_at
        itineraries.append(itin)

    itineraries.sort(key=lambda it: (not it.feasible, it.score))
    return itineraries[: config.top_n]
