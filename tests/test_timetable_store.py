"""Offline timetable artifact: save -> load must reproduce a composed Timetable
exactly (stops, trips, validities, footpaths) and route identically."""

from datetime import date, datetime, timedelta

import pytest

from travelplanner.models import CostLevel, Mode
from travelplanner.graph.schema import NodeType
from travelplanner.graph.validity import ServiceCalendar, Validity
from travelplanner.graph.scheduled import (
    ConnectionScan, Stop, StopTime, Timetable, Trip, load_timetable_artifact,
    make_trip, save_timetable)

SEASONAL = Validity(
    calendar=ServiceCalendar(start=date(2026, 1, 1), end=date(2026, 12, 31),
                             weekdays=frozenset({0, 1, 2, 3, 4})),
    open_months=frozenset({6, 7, 8}),
    forbidden_conditions=frozenset({"strike"}))


def _timetable() -> Timetable:
    tt = Timetable()
    tt.add_stop(Stop("A", "Aport", 52.30, 4.76, NodeType.AIRPORT,
                     tz="Europe/Amsterdam"))
    # min_transfer=None exercises a transfers.txt type-3 (forbidden) stop.
    tt.add_stop(Stop("B", "Bhof", 47.46, 8.55, NodeType.RAIL_STATION,
                     min_transfer=None, tz="Europe/Zurich"))
    tt.add_stop(Stop("C", "Ctown", 47.50, 8.60))                 # all defaults
    # A flight whose arrival offset carries a half-second, to prove float round-trip.
    tt.add_trip(Trip(
        id="FL", mode=Mode.FLIGHT, cost_level=CostLevel.HIGH,
        stop_times=(StopTime("A", timedelta(seconds=36000), timedelta(seconds=36000)),
                    StopTime("B", timedelta(seconds=41400, milliseconds=500),
                             timedelta(seconds=41400, milliseconds=500)))))
    tt.add_trip(make_trip("TR", Mode.TRAIN,
                          [("B", "12:00", "12:00"), ("C", "12:20", "12:20")],
                          validity=SEASONAL))
    tt.add_footpath("A", "C", timedelta(minutes=15))
    return tt


def test_roundtrip_preserves_timetable(tmp_path):
    tt = _timetable()
    path = str(tmp_path / "tt.json")
    save_timetable(tt, path)
    h = load_timetable_artifact(path)

    assert set(h.stops) == set(tt.stops)
    for sid, s in tt.stops.items():
        hs = h.stops[sid]
        assert (hs.name, hs.lat, hs.lon, hs.type, hs.min_transfer, hs.tz) == \
               (s.name, s.lat, s.lon, s.type, s.min_transfer, s.tz)

    assert set(h.trips) == set(tt.trips)
    for tid, t in tt.trips.items():
        ht = h.trips[tid]
        assert ht.mode is t.mode and ht.cost_level is t.cost_level
        assert ht.validity == t.validity                    # seasonal + conditions
        assert ht.stop_times == t.stop_times                # exact (float seconds)

    assert [(f.from_stop, f.to_stop, f.duration) for f in h.footpaths] == \
           [(f.from_stop, f.to_stop, f.duration) for f in tt.footpaths]


def test_loaded_artifact_routes_identically(tmp_path):
    tt = _timetable()
    path = str(tmp_path / "tt.json")
    save_timetable(tt, path)
    h = load_timetable_artifact(path)

    src = {"A": datetime(2026, 7, 1, 8, 0)}
    a = ConnectionScan(tt).query(src, "C")
    b = ConnectionScan(h).query(src, "C")
    assert (a is None) == (b is None)
    if a is not None:
        assert a.arrive == b.arrive
        assert [leg.mode for leg in a.legs] == [leg.mode for leg in b.legs]


def test_stale_format_rejected(tmp_path):
    import json
    path = tmp_path / "tt.json"
    save_timetable(_timetable(), str(path))
    data = json.loads(path.read_text())
    data["format_version"] = 99
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="rebuild"):
        load_timetable_artifact(str(path))
