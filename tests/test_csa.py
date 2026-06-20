"""Connection Scan Algorithm tests on synthetic timetables."""

from datetime import date, datetime, timedelta

from travelplanner.models import CostLevel, Mode
from travelplanner.graph.scheduled import (
    ConnectionScan,
    Stop,
    Timetable,
    make_trip,
)
from travelplanner.graph.validity import Validity

D = date(2026, 7, 1)


def dt(h, m, day=1):
    return datetime(2026, 7, day, h, m)


def _stop(tt, sid, transfer_min=5):
    tt.add_stop(Stop(id=sid, name=sid, lat=0.0, lon=0.0,
                     min_transfer=timedelta(minutes=transfer_min)))


def test_direct_trip_merges_into_one_leg():
    tt = Timetable()
    for s in "ABC":
        _stop(tt, s)
    tt.add_trip(make_trip("T1", Mode.TRAIN, [
        ("A", "09:00", "09:00"), ("B", "09:30", "09:32"),
        ("C", "10:00", "10:00")]))
    j = ConnectionScan(tt).query({"A": dt(8, 0)}, "C")
    assert j is not None
    assert j.arrive == dt(10, 0)
    assert len(j.legs) == 1                 # A..C on one run merged
    assert j.legs[0].from_stop == "A" and j.legs[0].to_stop == "C"
    assert j.transfers == 0


def test_picks_earliest_arriving_option():
    tt = Timetable()
    for s in "AC":
        _stop(tt, s)
    tt.add_trip(make_trip("slow", Mode.TRAIN, [
        ("A", "09:00", "09:00"), ("C", "10:00", "10:00")]))
    tt.add_trip(make_trip("fast", Mode.TRAIN, [
        ("A", "09:10", "09:10"), ("C", "09:50", "09:50")]))
    j = ConnectionScan(tt).query({"A": dt(8, 0)}, "C")
    assert j.arrive == dt(9, 50)
    assert j.legs[0].trip_id == "fast"


def test_tight_transfer_is_missed():
    tt = Timetable()
    for s in "AHB":
        _stop(tt, s, transfer_min=5)
    tt.add_trip(make_trip("T1", Mode.TRAIN, [
        ("A", "09:00", "09:00"), ("H", "09:30", "09:30")]))
    tt.add_trip(make_trip("T2tight", Mode.TRAIN, [
        ("H", "09:33", "09:33"), ("B", "10:00", "10:00")]))  # 3min < 5min
    # Short horizon isolates same-day behavior (no waiting a full day for the
    # next daily run).
    csa = ConnectionScan(tt, horizon=timedelta(hours=6))
    assert csa.query({"A": dt(8, 0)}, "B") is None


def test_sufficient_transfer_is_made():
    tt = Timetable()
    for s in "AHB":
        _stop(tt, s, transfer_min=5)
    tt.add_trip(make_trip("T1", Mode.TRAIN, [
        ("A", "09:00", "09:00"), ("H", "09:30", "09:30")]))
    tt.add_trip(make_trip("T2ok", Mode.TRAIN, [
        ("H", "09:36", "09:36"), ("B", "10:00", "10:00")]))  # 6min >= 5min
    j = ConnectionScan(tt).query({"A": dt(8, 0)}, "B")
    assert j is not None
    assert j.arrive == dt(10, 0)
    assert j.transfers == 1
    assert [leg.trip_id for leg in j.legs] == ["T1", "T2ok"]


def test_footpath_transfer_has_no_change_penalty():
    tt = Timetable()
    for s in ["A", "H1", "H2", "B"]:
        _stop(tt, s, transfer_min=5)
    tt.add_footpath("H1", "H2", timedelta(minutes=2))
    tt.add_trip(make_trip("T1", Mode.TRAIN, [
        ("A", "09:00", "09:00"), ("H1", "09:30", "09:30")]))
    # Departs H2 only 4 min after arriving H1; a 5-min change would miss it,
    # but the 2-min footpath leaves you ready, so it is caught.
    tt.add_trip(make_trip("T2", Mode.TRAIN, [
        ("H2", "09:34", "09:34"), ("B", "10:00", "10:00")]))
    j = ConnectionScan(tt).query({"A": dt(8, 0)}, "B")
    assert j is not None
    assert j.arrive == dt(10, 0)
    modes = [leg.mode for leg in j.legs]
    assert Mode.WALK in modes
    assert j.transfers == 1                 # two vehicle legs


def test_seasonal_ferry_only_runs_in_season():
    tt = Timetable()
    for s in "XY":
        _stop(tt, s)
    summer = Validity(open_months=frozenset({6, 7, 8, 9}))
    tt.add_trip(make_trip("ferry", Mode.FERRY, [
        ("X", "10:00", "10:00"), ("Y", "10:45", "10:45")], validity=summer))
    csa = ConnectionScan(tt)
    assert csa.query({"X": dt(9, 0)}, "Y").arrive == dt(10, 45)   # July
    winter = ConnectionScan(tt).query(
        {"X": datetime(2026, 1, 15, 9, 0)}, "Y")
    assert winter is None


def test_overnight_flight_crosses_midnight():
    tt = Timetable()
    for s in "PQ":
        _stop(tt, s)
    tt.add_trip(make_trip("F1", Mode.FLIGHT, [
        ("P", "23:00", "23:00"), ("Q", "26:00", "26:00")],  # arrive 02:00 +1
        cost_level=CostLevel.HIGH))
    j = ConnectionScan(tt).query({"P": dt(22, 0)}, "Q")
    assert j is not None
    assert j.arrive == datetime(2026, 7, 2, 2, 0)
    assert j.legs[0].mode is Mode.FLIGHT
    assert j.cost_level is CostLevel.HIGH


def test_multi_source_uses_best_origin():
    tt = Timetable()
    for s in ["A", "M", "C"]:
        _stop(tt, s)
    tt.add_trip(make_trip("long", Mode.TRAIN, [
        ("A", "09:00", "09:00"), ("C", "10:00", "10:00")]))
    tt.add_trip(make_trip("short", Mode.TRAIN, [
        ("M", "09:40", "09:40"), ("C", "09:50", "09:50")]))
    csa = ConnectionScan(tt)
    only_a = csa.arrival_times({"A": dt(8, 0)})
    both = csa.arrival_times({"A": dt(8, 0), "M": dt(9, 30)})
    assert only_a["C"] == dt(10, 0)
    assert both["C"] == dt(9, 50)           # closer source wins


def test_chained_footpaths_are_closed_by_engine():
    # B->C and C->T footpaths, no direct B->T. The engine should transitively
    # close them so a two-hop walk works (review Finding 2).
    tt = Timetable()
    for s in ["A", "B", "C", "T"]:
        _stop(tt, s)
    tt.add_footpath("B", "C", timedelta(minutes=2))
    tt.add_footpath("C", "T", timedelta(minutes=2))
    tt.add_trip(make_trip("T1", Mode.TRAIN, [
        ("A", "09:00", "09:00"), ("B", "09:10", "09:10")]))
    j = ConnectionScan(tt).query({"A": dt(8, 0)}, "T")
    assert j is not None
    assert j.arrive == dt(9, 14)            # 09:10 + 2 + 2 min walk


def test_degenerate_zero_duration_connection_is_dropped():
    # A connection whose arrival == departure is invalid data; routing over it
    # must not happen (review Finding 1 trigger removed).
    tt = Timetable()
    for s in "AB":
        _stop(tt, s)
    tt.add_trip(make_trip("bad", Mode.TRAIN, [
        ("A", "09:00", "09:00"), ("B", "09:00", "09:00")]))  # 0-duration hop
    csa = ConnectionScan(tt, horizon=timedelta(hours=6))
    assert csa.query({"A": dt(8, 0)}, "B") is None


def test_unreachable_returns_none():
    tt = Timetable()
    for s in "AB":
        _stop(tt, s)
    tt.add_trip(make_trip("T1", Mode.TRAIN, [
        ("A", "09:00", "09:00"), ("A", "09:30", "09:30")]))  # goes nowhere useful
    assert ConnectionScan(tt).query({"A": dt(8, 0)}, "B") is None


def test_ride_through_not_stitched_into_infeasible_transfer():
    """A faster run that overwrites an intermediate stop must not cause the
    boarded ride-through to be reconstructed as an infeasible transfer.

    T3 rides S4->S1->S0; a faster T5 reaches S1 13:20 but the only onward hop is
    T3's S1->S0 at 13:24 -- a 4-min change < S1's 5-min minimum. The correct (and
    only feasible) answer is to ride T3 straight through from S4, one leg, no
    transfer -- never the impossible T5->T3 change at S1.
    """
    tt = Timetable()
    _stop(tt, "S4", transfer_min=8)
    _stop(tt, "S1", transfer_min=5)
    _stop(tt, "S0", transfer_min=8)
    tt.add_trip(make_trip("T3", Mode.TRAIN, [
        ("S4", "13:00", "13:00"), ("S1", "13:24", "13:24"), ("S0", "13:37", "13:37")]))
    tt.add_trip(make_trip("T5", Mode.TRAIN, [
        ("S4", "13:00", "13:00"), ("S1", "13:20", "13:20")]))
    j = ConnectionScan(tt, horizon=timedelta(hours=6)).query({"S4": dt(12, 0)}, "S0")
    assert j is not None
    assert j.arrive == dt(13, 37)
    assert j.transfers == 0                          # ridden through, not stitched
    assert [leg.trip_id for leg in j.legs] == ["T3"]
    assert j.legs[0].from_stop == "S4" and j.legs[0].to_stop == "S0"


def test_unregistered_interior_stop_uses_default_transfer():
    """A stop a trip passes through but that was never add_stop()-ed must still
    enforce a sane change time (not zero), so an impossible 1-min change between
    two runs at that stop is rejected."""
    tt = Timetable()
    _stop(tt, "A", transfer_min=10)
    _stop(tt, "C", transfer_min=10)
    # B is referenced by the trips below but never registered.
    tt.add_trip(make_trip("R1", Mode.TRAIN, [("A", "09:00", "09:00"), ("B", "10:00", "10:00")]))
    tt.add_trip(make_trip("R2", Mode.TRAIN, [("B", "10:01", "10:01"), ("C", "10:30", "10:30")]))
    csa = ConnectionScan(tt, horizon=timedelta(hours=6))
    # 1-min change at B < 5-min default minimum -> no same-day journey.
    assert csa.query({"A": dt(8, 0)}, "C") is None


def test_arrival_times_empty_sources():
    """arrival_times({}) returns an empty dict, not a crash."""
    tt = Timetable()
    _stop(tt, "A")
    assert ConnectionScan(tt).arrival_times({}) == {}
