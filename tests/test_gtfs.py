"""GTFS loader tests on a synthetic feed written to a temp directory."""

import csv
from datetime import datetime

from travelplanner.models import Mode
from travelplanner.graph.scheduled import ConnectionScan, load_timetable


def _write(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def _build_feed(d):
    _write(d / "stops.txt", [
        {"stop_id": "A", "stop_name": "Aville", "stop_lat": "46.9", "stop_lon": "7.4"},
        {"stop_id": "B", "stop_name": "Bborg", "stop_lat": "46.7", "stop_lon": "7.6"},
        {"stop_id": "C", "stop_name": "Ctown", "stop_lat": "46.3", "stop_lon": "8.0"},
        {"stop_id": "X", "stop_name": "Xport", "stop_lat": "46.4", "stop_lon": "6.8"},
        {"stop_id": "Y", "stop_name": "Yisle", "stop_lat": "46.5", "stop_lon": "6.9"},
    ])
    _write(d / "routes.txt", [
        {"route_id": "R1", "route_type": "2"},   # rail
        {"route_id": "R2", "route_type": "4"},   # ferry
    ])
    _write(d / "calendar.txt", [
        # Weekdays Mon-Fri, all year.
        {"service_id": "WD", "monday": "1", "tuesday": "1", "wednesday": "1",
         "thursday": "1", "friday": "1", "saturday": "0", "sunday": "0",
         "start_date": "20260101", "end_date": "20261231"},
        # Ferry: every day, summer only.
        {"service_id": "SUMMER", "monday": "1", "tuesday": "1", "wednesday": "1",
         "thursday": "1", "friday": "1", "saturday": "1", "sunday": "1",
         "start_date": "20260601", "end_date": "20260930"},
    ])
    _write(d / "calendar_dates.txt", [
        # Add an extra WD run on Sunday 2026-07-05.
        {"service_id": "WD", "date": "20260705", "exception_type": "1"},
    ])
    _write(d / "trips.txt", [
        {"route_id": "R1", "service_id": "WD", "trip_id": "TR1"},
        {"route_id": "R2", "service_id": "SUMMER", "trip_id": "FR1"},
    ])
    _write(d / "stop_times.txt", [
        {"trip_id": "TR1", "stop_sequence": "1", "stop_id": "A",
         "arrival_time": "09:00:00", "departure_time": "09:00:00"},
        {"trip_id": "TR1", "stop_sequence": "2", "stop_id": "B",
         "arrival_time": "09:30:00", "departure_time": "09:32:00"},
        {"trip_id": "TR1", "stop_sequence": "3", "stop_id": "C",
         "arrival_time": "10:00:00", "departure_time": "10:00:00"},
        {"trip_id": "FR1", "stop_sequence": "1", "stop_id": "X",
         "arrival_time": "11:00:00", "departure_time": "11:00:00"},
        {"trip_id": "FR1", "stop_sequence": "2", "stop_id": "Y",
         "arrival_time": "11:45:00", "departure_time": "11:45:00"},
    ])


def test_loads_stops_trips_and_modes(tmp_path):
    _build_feed(tmp_path)
    tt = load_timetable(str(tmp_path))
    assert set(tt.stops) == {"A", "B", "C", "X", "Y"}
    assert tt.trips["TR1"].mode is Mode.TRAIN
    assert tt.trips["FR1"].mode is Mode.FERRY


def test_weekday_service_active_and_inactive(tmp_path):
    _build_feed(tmp_path)
    csa = ConnectionScan(load_timetable(str(tmp_path)))
    # 2026-07-01 is a Wednesday: rail runs.
    wed = csa.query({"A": datetime(2026, 7, 1, 8, 0)}, "C")
    assert wed is not None and wed.arrive == datetime(2026, 7, 1, 10, 0)
    # 2026-07-04 is a Saturday: WD service inactive (short horizon to avoid
    # rolling into Monday).
    from datetime import timedelta
    sat = ConnectionScan(load_timetable(str(tmp_path)),
                         horizon=timedelta(hours=12)).query(
        {"A": datetime(2026, 7, 4, 8, 0)}, "C")
    assert sat is None


def test_calendar_dates_adds_sunday_run(tmp_path):
    _build_feed(tmp_path)
    csa = ConnectionScan(load_timetable(str(tmp_path)))
    # 2026-07-05 is a Sunday, normally no WD service, but added via exception.
    sun = csa.query({"A": datetime(2026, 7, 5, 8, 0)}, "C")
    assert sun is not None and sun.arrive == datetime(2026, 7, 5, 10, 0)


def test_seasonal_ferry_summer_only(tmp_path):
    _build_feed(tmp_path)
    tt = load_timetable(str(tmp_path))
    summer = ConnectionScan(tt).query({"X": datetime(2026, 7, 1, 10, 0)}, "Y")
    assert summer is not None and summer.legs[0].mode is Mode.FERRY
    from datetime import timedelta
    winter = ConnectionScan(tt, horizon=timedelta(days=1)).query(
        {"X": datetime(2026, 1, 15, 10, 0)}, "Y")
    assert winter is None
