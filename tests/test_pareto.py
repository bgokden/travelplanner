"""Phase 4: multi-criteria Pareto frontier + objective selection."""

from datetime import datetime, timedelta

from travelplanner import place
from travelplanner.fares import heuristic_fare_model, reset_fare_model, set_fare_model
from travelplanner.models import CostLevel, Itinerary, Leg, Location, LocationType, Mode
from travelplanner.graph.schema import NodeType
from travelplanner.graph.scheduled import Stop, Timetable, make_trip
from travelplanner.graph.query import Objective
from travelplanner.graph.coupling import GeometricConnector, plan
from travelplanner.graph.coupling.planner import _dedupe, _order_key, _rank

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
        cost_level=CostLevel.LOW))
    tt.add_trip(make_trip("R2", Mode.TRAIN, [
        ("Mid", "11:10", "11:10"), ("Sy", "13:00", "13:00")],
        cost_level=CostLevel.LOW))
    return tt


ORIGIN = place("near X", LocationType.HOTEL, 47.0, 7.005)
DEST = place("near Y", LocationType.HOTEL, 45.0, 9.01)


def _priced_itin(fare, minutes, band):
    a = place("a", LocationType.CITY, 47.0, 7.0)
    b = place("b", LocationType.CITY, 47.5, 7.5)
    leg = Leg(Mode.CAR, a, b, 50.0, timedelta(minutes=minutes), timedelta(),
              band, fare_estimate=fare, fare_currency="EUR")
    return Itinerary([leg], DEP, 1.0)


def test_cheapest_ranks_by_fare_not_just_band():
    # Two MEDIUM-band itineraries with different fares: CHEAPEST orders by the
    # continuous fare, so the cheaper-but-slower one leads. The old 3-level band
    # tied them and fell back to time, which would have led with pricier-but-faster.
    cheap = _priced_itin(fare=10.0, minutes=120, band=CostLevel.MEDIUM)
    pricey = _priced_itin(fare=50.0, minutes=60, band=CostLevel.MEDIUM)
    ordered = sorted([pricey, cheap], key=_order_key(Objective.CHEAPEST))
    assert ordered[0] is cheap
    assert cheap.cost_level is pricey.cost_level is CostLevel.MEDIUM   # same band


def test_cheapest_follows_fare_not_band_end_to_end():
    # Full plan() path: invert the usual price order with a custom model so the
    # HIGH-band flight is the cheaper-by-fare option and the LOW-band train is
    # pricier. CHEAPEST must lead with the flight -- a result the old band-based
    # ranking could never give (it ordered the LOW band first).
    tt = _trade_off_timetable()
    conn = GeometricConnector(tt.stops)
    set_fare_model(heuristic_fare_model(rates={
        Mode.FLIGHT: (0.0, 0.01), Mode.TRAIN: (0.0, 1.00),
        Mode.CAR: (0.0, 0.15), Mode.WALK: (0.0, 0.0)}))
    try:
        cheapest = plan(ORIGIN, DEST, DEP, tt, conn,
                        objective=Objective.CHEAPEST, top_n=5)
    finally:
        reset_fare_model()
    assert cheapest[0].primary_mode is Mode.FLIGHT          # cheaper by fare
    assert cheapest[0].cost_level is CostLevel.HIGH         # despite the HIGH band


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


def _same_cost_walk_timetable():
    """A fast flight AND a slower train, BOTH car-free (walk access) and at the
    SAME cost tier -- so the flight dominates the train on every NON-emissions
    axis (time, cost, transfers, car_km). Only an emissions Pareto axis keeps the
    greener train on the frontier."""
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
        cost_level=CostLevel.HIGH))           # SAME cost as the flight
    return tt


def test_greenest_keeps_dominated_greener_train_on_frontier():
    # Regression: emissions was only a GREENEST sort tiebreaker, not a Pareto
    # axis, so a same-cost car-free flight DOMINATED the slower car-free train on
    # (time, cost, transfers, car_km) and _pareto dropped the train before the
    # emissions key could rank it -- GREENEST then returned the flight. With
    # emissions as a Pareto axis the greener train survives and GREENEST wins it.
    tt = _same_cost_walk_timetable()
    conn = GeometricConnector(tt.stops)
    green = plan(ORIGIN, DEST, DEP, tt, conn, objective=Objective.GREENEST, top_n=5)
    assert green[0].primary_mode is Mode.TRAIN
    assert not any(leg.mode is Mode.CAR for leg in green[0].legs)
    assert Mode.TRAIN in {it.primary_mode for it in green}   # survived the frontier
    air = plan(ORIGIN, DEST, DEP, tt, conn, objective=Objective.AIR_PRIORITY)[0]
    assert air.primary_mode is Mode.FLIGHT                    # air priority still flies


def test_low_car_option_survives_frontier_under_greenest():
    """The low-driving option is on the GREENEST frontier (not pruned) even though
    it is slower than the flight. Under non-green objectives it is a strictly
    worse-on-(time,cost,transfers) option and is correctly dropped."""
    tt = _drive_vs_walk_timetable()
    conn = GeometricConnector(tt.stops)
    primaries = {it.primary_mode for it in
                 plan(ORIGIN, DEST, DEP, tt, conn,
                      objective=Objective.GREENEST, top_n=5)}
    assert Mode.FLIGHT in primaries and Mode.TRAIN in primaries


def test_cheapest_picks_train():
    tt = _trade_off_timetable()
    conn = GeometricConnector(tt.stops)
    results = plan(ORIGIN, DEST, DEP, tt, conn, objective=Objective.CHEAPEST)
    assert results[0].primary_mode is Mode.TRAIN
    assert results[0].cost_level is CostLevel.LOW   # train is the cheapest mode


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


def test_objective_frontier_excludes_green_only_survivor_from_fastest():
    # A car-free, low-emission train that is slower AND the same cost as a flight
    # is dominated on (time, cost, transfers). It must NOT occupy a FASTEST slot
    # (kept only by the green axes), but GREENEST -- which ranks emissions -- keeps
    # it on the frontier.
    flight = _itin([_leg(Mode.FLIGHT, 500.0, 3000, CostLevel.MEDIUM)])
    train = _itin([_leg(Mode.TRAIN, 480.0, 6000, CostLevel.MEDIUM)])   # slower, same cost
    fast = _rank([flight, train], Objective.FASTEST, top_n=5)
    assert flight in fast and train not in fast
    green = _rank([flight, train], Objective.GREENEST, top_n=5)
    assert train in green                          # lower emissions -> kept


def test_air_priority_keeps_car_free_flight_over_faster_drive():
    # Regression: the objective-aware frontier pruned a car-free flight that was
    # slower AND pricier than a drive-access alternative (dominated on time/cost/
    # transfers), so AIR_PRIORITY returned NO flight -- the persona bug again.
    # AIR_PRIORITY keeps the full frontier, so the car-free flight survives.
    flight = _itin([_leg(Mode.WALK, 0.5, 300), _leg(Mode.FLIGHT, 500.0, 7200, CostLevel.HIGH)])
    drive = _itin([_leg(Mode.CAR, 300.0, 3600, CostLevel.LOW)])   # faster + cheaper
    air = _rank([flight, drive], Objective.AIR_PRIORITY, top_n=5)
    assert any(leg.mode is Mode.FLIGHT for it in air for leg in it.legs)
    assert air[0].primary_mode is Mode.FLIGHT                     # air ranked first
    # FASTEST still drops the slower flight (it is genuinely worse on its axes)
    fast = _rank([flight, drive], Objective.FASTEST, top_n=5)
    assert not any(leg.mode is Mode.FLIGHT for it in fast for leg in it.legs)


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
