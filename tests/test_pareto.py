"""Phase 4: multi-criteria Pareto frontier + objective selection."""

from datetime import datetime

from travelplanner import place
from travelplanner.models import CostLevel, LocationType, Mode
from travelplanner.graph.schema import NodeType
from travelplanner.graph.scheduled import Stop, Timetable, make_trip
from travelplanner.graph.query import Objective
from travelplanner.graph.coupling import GeometricConnector, plan

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
