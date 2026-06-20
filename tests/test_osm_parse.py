"""Pure-function tests for the OSM loader's tag parsers (no PBF needed)."""

from datetime import date

from travelplanner.graph.road.osm import (
    is_car_ferry,
    parse_duration,
    parse_maxspeed,
    parse_seasonal_closure,
)


def test_maxspeed_numeric():
    assert parse_maxspeed("50", 30) == 50.0


def test_maxspeed_mph_converts():
    assert round(parse_maxspeed("60 mph", 30)) == 97


def test_maxspeed_unparseable_falls_back():
    assert parse_maxspeed("RO:urban", 30) == 30
    assert parse_maxspeed(None, 42) == 42


def test_seasonal_closure_winter_range():
    v = parse_seasonal_closure(
        {"motor_vehicle:conditional": "no @ (Nov-May)"})
    # Closed Nov-May -> open Jun-Oct.
    assert v.open_months == frozenset({6, 7, 8, 9, 10})
    assert v.is_active(date(2026, 7, 1)) is True
    assert v.is_active(date(2026, 12, 1)) is False


def test_seasonal_closure_access_key():
    v = parse_seasonal_closure({"access:conditional": "no @ (Dec-Mar)"})
    assert v.open_months == frozenset({4, 5, 6, 7, 8, 9, 10, 11})


def test_no_closure_is_unrestricted():
    v = parse_seasonal_closure({"highway": "primary", "maxspeed": "80"})
    assert v.open_months == frozenset()
    assert v.is_active(date(2026, 1, 1)) is True


# Regression cases taken from real Swiss OSM pass tags (Overpass, 2026-06).
def test_real_grimsel_furka_oct_may():
    # Grimselstrasse / Furkastrasse: closed Oct-May, open Jun-Sep.
    v = parse_seasonal_closure({"motor_vehicle:conditional": "no @ (Oct-May)"})
    assert v.open_months == frozenset({6, 7, 8, 9})


def test_real_lowercase_months():
    v = parse_seasonal_closure({"access:conditional": "no @ (dec-mar)"})
    assert v.open_months == frozenset({4, 5, 6, 7, 8, 9, 10, 11})


def test_real_trailing_time_spec_ignored():
    # Oberalppass: "no @ (Dec-Mar Mo-Su 00:00-24:00)" -> month range only.
    v = parse_seasonal_closure(
        {"motor_vehicle:conditional": "no @ (Dec-Mar Mo-Su 00:00-24:00)"})
    assert v.open_months == frozenset({4, 5, 6, 7, 8, 9, 10, 11})


# --- ferries --------------------------------------------------------------

def test_is_car_ferry_requires_car_access():
    assert is_car_ferry({"route": "ferry", "motorcar": "yes"})
    assert is_car_ferry({"route": "ferry", "motor_vehicle": "yes"})
    assert is_car_ferry({"route": "ferry", "motor_vehicle": "designated"})
    # passenger-only ferry: not for a car graph
    assert not is_car_ferry({"route": "ferry", "foot": "yes"})
    assert not is_car_ferry({"route": "ferry"})
    # explicit no wins even if the other key is positive
    assert not is_car_ferry(
        {"route": "ferry", "motorcar": "no", "motor_vehicle": "yes"})
    # not a ferry at all
    assert not is_car_ferry({"highway": "primary"})


def test_parse_duration_formats():
    assert parse_duration("1:30") == 5400.0          # HH:MM
    assert parse_duration("0:35") == 2100.0
    assert parse_duration("00:35:30") == 2130.0      # HH:MM:SS
    assert parse_duration("35") == 2100.0            # bare minutes
    assert parse_duration("12.5") == 750.0
    assert parse_duration(None) is None
    assert parse_duration("") is None
    assert parse_duration("soon") is None


def test_parse_duration_rejects_nonpositive_and_signed():
    # non-positive or signed durations are rejected so no zero/negative arc forms
    assert parse_duration("0") is None
    assert parse_duration("00:00:00") is None
    assert parse_duration("-1:30") is None
    assert parse_duration("1:-30") is None
    assert parse_duration("-5") is None
    assert parse_duration("1:2:3:4") is None
