"""GTFS loader tests on a synthetic feed written to a temp directory."""

import csv
from datetime import date, datetime

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


def _minimal(tmp_path, trips, stop_times, *, service="EVERY"):
    _write(tmp_path / "stops.txt", [
        {"stop_id": "A", "stop_name": "A", "stop_lat": "47.0", "stop_lon": "7.0"},
        {"stop_id": "B", "stop_name": "B", "stop_lat": "46.5", "stop_lon": "7.5"},
        {"stop_id": "C", "stop_name": "C", "stop_lat": "46.0", "stop_lon": "8.0"}])
    _write(tmp_path / "routes.txt", [{"route_id": "R", "route_type": "2"}])
    _write(tmp_path / "calendar.txt", [
        {"service_id": service, "monday": "1", "tuesday": "1", "wednesday": "1",
         "thursday": "1", "friday": "1", "saturday": "1", "sunday": "1",
         "start_date": "20260101", "end_date": "20261231"}])
    _write(tmp_path / "trips.txt", trips)
    _write(tmp_path / "stop_times.txt", stop_times)


def test_empty_interior_stop_time_does_not_crash(tmp_path):
    """A non-timepoint interior stop with empty times must not crash the load;
    it is dropped and the trip keeps its timed stops."""
    _minimal(tmp_path,
             [{"route_id": "R", "service_id": "EVERY", "trip_id": "T"}],
             [{"trip_id": "T", "stop_sequence": "1", "stop_id": "A",
               "arrival_time": "09:00:00", "departure_time": "09:00:00"},
              {"trip_id": "T", "stop_sequence": "2", "stop_id": "B",
               "arrival_time": "", "departure_time": ""},
              {"trip_id": "T", "stop_sequence": "3", "stop_id": "C",
               "arrival_time": "10:00:00", "departure_time": "10:00:00"}])
    tt = load_timetable(str(tmp_path))          # must not raise
    assert "T" in tt.trips
    j = ConnectionScan(tt).query({"A": datetime(2026, 7, 1, 8, 0)}, "C")
    assert j is not None and j.arrive == datetime(2026, 7, 1, 10, 0)


def test_dangling_service_id_trip_is_dropped(tmp_path):
    """A trip whose service_id is not defined must not run every day; it is
    dropped rather than given an always-active empty Validity."""
    _minimal(tmp_path,
             [{"route_id": "R", "service_id": "GHOST", "trip_id": "T"}],  # GHOST undefined
             [{"trip_id": "T", "stop_sequence": "1", "stop_id": "A",
               "arrival_time": "09:00:00", "departure_time": "09:00:00"},
              {"trip_id": "T", "stop_sequence": "2", "stop_id": "C",
               "arrival_time": "10:00:00", "departure_time": "10:00:00"}])
    tt = load_timetable(str(tmp_path))
    assert "T" not in tt.trips
    assert ConnectionScan(tt).query({"A": datetime(2026, 7, 1, 8, 0)}, "C") is None


# --- loader robustness (review: real-world GTFS feed quirks) ----------------

def _min_stops_routes(d):
    _write(d / "stops.txt", [
        {"stop_id": "A", "stop_name": "A", "stop_lat": "47.0", "stop_lon": "7.0"},
        {"stop_id": "B", "stop_name": "B", "stop_lat": "46.0", "stop_lon": "8.0"}])
    _write(d / "routes.txt", [{"route_id": "R", "route_type": "2"}])
    _write(d / "stop_times.txt", [
        {"trip_id": "T", "stop_sequence": "1", "stop_id": "A",
         "arrival_time": "09:00:00", "departure_time": "09:00:00"},
        {"trip_id": "T", "stop_sequence": "2", "stop_id": "B",
         "arrival_time": "10:00:00", "departure_time": "10:00:00"}])


def test_water_route_type_is_ferry():
    from travelplanner.graph.scheduled.gtfs import _mode_for
    assert _mode_for(1000) is Mode.FERRY and _mode_for(1099) is Mode.FERRY
    assert _mode_for(1200) is Mode.FERRY and _mode_for(4) is Mode.FERRY
    assert _mode_for(2) is Mode.TRAIN and _mode_for(1100) is Mode.FLIGHT


def test_malformed_exception_type_not_treated_as_removal(tmp_path):
    _build_feed(tmp_path)
    # an empty exception_type on an active weekday must NOT silently cancel it
    _write(tmp_path / "calendar_dates.txt", [
        {"service_id": "WD", "date": "20260701", "exception_type": ""}])  # a Wednesday
    csa = ConnectionScan(load_timetable(str(tmp_path)))
    assert csa.query({"A": datetime(2026, 7, 1, 8, 0)}, "C") is not None


def test_calendar_dates_only_removal_does_not_run_every_day(tmp_path):
    # The only calendar info is a removal -> the service has no active days and the
    # trip must not become always-active.
    _min_stops_routes(tmp_path)
    _write(tmp_path / "calendar_dates.txt", [
        {"service_id": "S", "date": "20260701", "exception_type": "2"}])
    _write(tmp_path / "trips.txt", [
        {"route_id": "R", "service_id": "S", "trip_id": "T"}])
    tt = load_timetable(str(tmp_path))
    assert "T" not in tt.trips


def test_no_calendar_feed_defaults_active(tmp_path):
    # No calendar files at all -> trips default to always-active (unchanged).
    _min_stops_routes(tmp_path)
    _write(tmp_path / "trips.txt", [
        {"route_id": "R", "service_id": "S", "trip_id": "T"}])
    tt = load_timetable(str(tmp_path))
    assert "T" in tt.trips and tt.trips["T"].validity.is_active(date(2026, 7, 1))


def test_loader_tolerates_float_and_malformed_values(tmp_path):
    _build_feed(tmp_path)
    _write(tmp_path / "routes.txt", [
        {"route_id": "R1", "route_type": "2.0"},   # float-exported numeric
        {"route_id": "R2", "route_type": "bus"}])   # non-numeric -> generic TRAIN
    _write(tmp_path / "stop_times.txt", [
        {"trip_id": "TR1", "stop_sequence": "1.0", "stop_id": "A",
         "arrival_time": "09:00:00", "departure_time": "09:00:00"},
        {"trip_id": "TR1", "stop_sequence": "2.0", "stop_id": "B",
         "arrival_time": "09:30:00", "departure_time": "09:30:00"},
        {"trip_id": "TR1", "stop_sequence": "3.0", "stop_id": "C",
         "arrival_time": "10:00:00", "departure_time": "10:00:00"}])
    tt = load_timetable(str(tmp_path))     # no crash
    assert [st.stop_id for st in tt.trips["TR1"].stop_times] == ["A", "B", "C"]


def test_loader_skips_unparseable_stop_coords(tmp_path):
    _build_feed(tmp_path)
    _write(tmp_path / "stops.txt", [
        {"stop_id": "A", "stop_name": "A", "stop_lat": "N/A", "stop_lon": "7.0"},
        {"stop_id": "B", "stop_name": "B", "stop_lat": "46.0", "stop_lon": "8.0"}])
    tt = load_timetable(str(tmp_path))     # no crash
    assert "A" not in tt.stops and "B" in tt.stops


def test_loader_reads_latin1_feed(tmp_path):
    _build_feed(tmp_path)
    rows = ("stop_id,stop_name,stop_lat,stop_lon\r\n"
            "A,Zürich,47.0,7.0\r\nB,Genève,46.0,8.0\r\n"
            "C,Ctown,46.3,8.0\r\nX,Xp,46.4,6.8\r\nY,Yi,46.5,6.9\r\n")
    (tmp_path / "stops.txt").write_bytes(rows.encode("latin-1"))
    tt = load_timetable(str(tmp_path))     # no crash on the non-UTF-8 bytes
    assert tt.stops["A"].name == "Zürich"
