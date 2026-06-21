"""Phase 4: multi-criteria Pareto frontier + objective selection."""

from datetime import datetime, timedelta

from travelplanner import place
from travelplanner.models import CostLevel, Itinerary, Leg, Location, LocationType, Mode
from travelplanner.graph.schema import NodeType
from travelplanner.graph.scheduled import Stop, Timetable, make_trip
from travelplanner.graph.query import Objective
from travelplanner.graph.coupling import GeometricConnector, plan
from travelplanner.graph.coupling.planner import _dedupe, _order_key

DEP = datetime(2026, 7, 1, 8, 0)


def _trade_off_timetable():
    """Long trip with a fast pricey flight (0 transfers) and a slower cheaper
    train with one change."""
    tt = Timetable()
    tt.add_stop(Stop("AxAir", "X airport", 47.0, 7.00, NodeType.AIRPORT))
    tt.add_stop(Stop("AyAir", "Y airport", 45.0, 9.00, NodeType.AIRPORT))
    tt.add_stop(Stop("Sx", "X station", 47.0, 7.02))
    tt.add_stop(Stop("Mid", "Mid station", 46.0, 8.0))
    tt.add_stop(Stop("Sy", "Y station", 45.0, 9.02))
    tt.add_trip(make_trip("FL", Mode.FLIGHT, [
        ("AxAir", "09:00", "09:00"), ("AyAir", "10:00", "10:00")],
        cost_level=CostLevel.HIGH))
    tt.add_trip(make_trip("R1", Mode.TRAIN, [
        ("Sx", "09:00", "09:00"), ("Mid", "11:00", "11:00")],
        cost_level=CostLevel.MEDIUM))
    tt.add_trip(make_trip("R2", Mode.TRAIN, [
        ("Mid", "11:10", "11:10"), ("Sy", "13:00", "13:00")],
        cost_level=CostLevel.MEDIUM))
    return tt


ORIGIN = place("near X", LocationType.HOTEL, 47.0, 7.005)
DEST = place("near Y", LocationType.HOTEL, 45.0, 9.01)


def test_frontier_contains_both_flight_and_train():
    tt = _trade_off_timetable()
    conn = GeometricConnector(tt.stops)
    results = plan(ORIGIN, DEST, DEP, tt, conn, top_n=5)
    primaries = {it.primary_mode for it in results}
    assert Mode.FLIGHT in primaries
    assert Mode.TRAIN in primaries


def test_air_priority_picks_flight():
    tt = _trade_off_timetable()
    conn = GeometricConnector(tt.stops)
    results = plan(ORIGIN, DEST, DEP, tt, conn, objective=Objective.AIR_PRIORITY)
    assert results[0].primary_mode is Mode.FLIGHT


def _drive_vs_walk_timetable():
    """A fast flight reached only by driving to a far airport, vs a slower train
    reached on foot from a near station -- so the train uses no private car."""
    tt = Timetable()
    tt.add_stop(Stop("FarAirO", "origin airport", 47.0, 7.30, NodeType.AIRPORT))
    tt.add_stop(Stop("FarAirD", "dest airport", 45.0, 9.30, NodeType.AIRPORT))
    tt.add_stop(Stop("NearStO", "origin station", 47.0, 7.005))
    tt.add_stop(Stop("NearStD", "dest station", 45.0, 9.005))
    tt.add_trip(make_trip("FLT", Mode.FLIGHT, [
        ("FarAirO", "09:00", "09:00"), ("FarAirD", "10:00", "10:00")],
        cost_level=CostLevel.HIGH))
    tt.add_trip(make_trip("TRN", Mode.TRAIN, [
        ("NearStO", "09:00", "09:00"), ("NearStD", "13:00", "13:00")],
        cost_level=CostLevel.MEDIUM))
    return tt


def test_greenest_minimizes_driving():
    """GREENEST keeps and prefers the low-car (train) option that AIR_PRIORITY
    and FASTEST rank below the drive-to-airport flight."""
    tt = _drive_vs_walk_timetable()
    conn = GeometricConnector(tt.stops)
    green = plan(ORIGIN, DEST, DEP, tt, conn, objective=Objective.GREENEST)[0]
    air = plan(ORIGIN, DEST, DEP, tt, conn, objective=Objective.AIR_PRIORITY)[0]
    assert air.primary_mode is Mode.FLIGHT
    assert green.primary_mode is Mode.TRAIN
    assert not any(leg.mode is Mode.CAR for leg in green.legs)


def _walk_to_both_timetable():
    """A fast flight AND a slower train both reachable ON FOOT (no car), so
    private-car distance ties at 0 -- only emissions tells them apart."""
    tt = Timetable()
    tt.add_stop(Stop("AirO", "origin airport", 47.0, 7.006, NodeType.AIRPORT))
    tt.add_stop(Stop("AirD", "dest airport", 45.0, 9.006, NodeType.AIRPORT))
    tt.add_stop(Stop("StO", "origin station", 47.0, 7.004))
    tt.add_stop(Stop("StD", "dest station", 45.0, 9.004))
    tt.add_trip(make_trip("FLT", Mode.FLIGHT, [
        ("AirO", "09:00", "09:00"), ("AirD", "10:00", "10:00")],
        cost_level=CostLevel.HIGH))
    tt.add_trip(make_trip("TRN", Mode.TRAIN, [
        ("StO", "09:00", "09:00"), ("StD", "12:00", "12:00")],
        cost_level=CostLevel.MEDIUM))
    return tt


def test_greenest_prefers_train_over_flight_when_both_car_free():
    # The reported bug: greenest only minimized car-km, so a car-free flight and
    # a car-free train tied and the faster flight won. Now emissions rank train.
    tt = _walk_to_both_timetable()
    conn = GeometricConnector(tt.stops)
    green = plan(ORIGIN, DEST, DEP, tt, conn, objective=Objective.GREENEST)[0]
    air = plan(ORIGIN, DEST, DEP, tt, conn, objective=Objective.AIR_PRIORITY)[0]
    assert air.primary_mode is Mode.FLIGHT       # air priority still flies
    assert green.primary_mode is Mode.TRAIN      # greenest avoids the flight
    assert not any(leg.mode is Mode.CAR for leg in green.legs)


def test_low_car_option_survives_frontier():
    """The low-driving option is on the Pareto frontier (not pruned) even though
    it has more transfers and is slower than the flight."""
    tt = _drive_vs_walk_timetable()
    conn = GeometricConnector(tt.stops)
    primaries = {it.primary_mode for it in plan(ORIGIN, DEST, DEP, tt, conn, top_n=5)}
    assert Mode.FLIGHT in primaries and Mode.TRAIN in primaries


def test_cheapest_picks_train():
    tt = _trade_off_timetable()
    conn = GeometricConnector(tt.stops)
    results = plan(ORIGIN, DEST, DEP, tt, conn, objective=Objective.CHEAPEST)
    assert results[0].primary_mode is Mode.TRAIN
    assert results[0].cost_level is CostLevel.MEDIUM


def test_fastest_picks_flight():
    tt = _trade_off_timetable()
    conn = GeometricConnector(tt.stops)
    results = plan(ORIGIN, DEST, DEP, tt, conn, objective=Objective.FASTEST)
    assert results[0].primary_mode is Mode.FLIGHT


def test_strictly_dominated_flight_is_dropped():
    # Flight is slower, pricier, and no fewer transfers than a direct train:
    # it is dominated and must not be selected even under AIR_PRIORITY.
    tt = Timetable()
    tt.add_stop(Stop("AxAir", "X airport", 47.0, 7.00, NodeType.AIRPORT))
    tt.add_stop(Stop("AyAir", "Y airport", 45.0, 9.00, NodeType.AIRPORT))
    tt.add_stop(Stop("Sx", "X station", 47.0, 7.02))
    tt.add_stop(Stop("Sy", "Y station", 45.0, 9.02))
    tt.add_trip(make_trip("FL", Mode.FLIGHT, [
        ("AxAir", "09:00", "09:00"), ("AyAir", "14:00", "14:00")],
        cost_level=CostLevel.HIGH))
    tt.add_trip(make_trip("R", Mode.TRAIN, [
        ("Sx", "09:00", "09:00"), ("Sy", "11:00", "11:00")],
        cost_level=CostLevel.MEDIUM))
    conn = GeometricConnector(tt.stops)
    results = plan(ORIGIN, DEST, DEP, tt, conn, objective=Objective.AIR_PRIORITY)
    assert results[0].primary_mode is Mode.TRAIN
    assert all(it.primary_mode is not Mode.FLIGHT for it in results)


# --- ranking-function regressions (review: core planner bug hunt) -----------

def _leg(mode, km, secs, cost=CostLevel.MEDIUM):
    loc = Location("x", LocationType.LANDMARK, 0.0, 0.0)
    return Leg(mode=mode, from_loc=loc, to_loc=loc, distance_km=km,
               travel_time=timedelta(seconds=secs), overhead=timedelta(),
               cost_level=cost)


def _itin(legs):
    return Itinerary(legs=legs, depart_at=datetime(2026, 7, 1, 8, 0), score=0.0)


def test_dedupe_keeps_distinct_cost_and_car_km():
    """Two same-mode, same-duration itineraries differing on cost / car distance
    are both kept (the cheaper/greener one is not collapsed away)."""
    a = _itin([_leg(Mode.CAR, 100.0, 3600, CostLevel.HIGH)])
    b = _itin([_leg(Mode.CAR, 10.0, 3600, CostLevel.LOW)])
    assert len(_dedupe([a, b])) == 2


def test_dedupe_collapses_true_duplicates():
    a = _itin([_leg(Mode.CAR, 50.0, 3600, CostLevel.MEDIUM)])
    b = _itin([_leg(Mode.CAR, 50.0, 3600, CostLevel.MEDIUM)])
    assert len(_dedupe([a, b])) == 1


def test_air_priority_counts_flight_leg_not_longest_leg():
    """A genuine flight whose airport-access drive is its longest leg must still
    rank as 'air' under AIR_PRIORITY (it flies)."""
    flight = _itin([_leg(Mode.CAR, 80.0, 3600), _leg(Mode.FLIGHT, 40.0, 3600),
                    _leg(Mode.WALK, 0.5, 300)])
    train = _itin([_leg(Mode.TRAIN, 200.0, 7200)])
    assert flight.primary_mode is Mode.CAR          # longest leg is the drive
    key = _order_key(Objective.AIR_PRIORITY)
    assert key(flight)[0] == 0                       # still treated as air
    assert key(train)[0] == 1
    assert sorted([train, flight], key=key)[0] is flight
