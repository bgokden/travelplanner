from datetime import datetime, timedelta

from travelplanner import city, estimate, place
from travelplanner.models import LocationType, Mode
from travelplanner.modes import PlannerConfig

START = datetime(2026, 7, 1, 8, 0)


def test_default_config_prioritizes_air_on_close_hop():
    # London-Paris (~344 km) is a genuine close call: ground and air totals are
    # within an hour. The default air bonus is meant to tip such cases to air.
    origin = city("London")
    dest = city("Paris")
    results = estimate(origin, dest, START)
    assert results
    assert results[0].primary_mode is Mode.FLIGHT


def test_long_haul_prefers_air():
    origin = city("New York")
    dest = city("Tokyo")
    results = estimate(origin, dest, START)
    assert results[0].primary_mode is Mode.FLIGHT


def test_air_suppressed_below_min_distance():
    origin = place("Hotel A", LocationType.HOTEL, 40.7128, -74.0060)
    dest = place("Hotel B", LocationType.HOTEL, 40.7300, -74.0100)
    results = estimate(origin, dest, START)
    assert all(it.primary_mode is not Mode.FLIGHT for it in results)


def test_air_bonus_tips_close_call():
    # London-Paris: ground (train) and air totals are close enough that the
    # air bonus is the deciding factor.
    origin = city("London")
    dest = city("Paris")
    low_bonus = estimate(origin, dest, START, config=PlannerConfig(air_bonus=0.0))
    high_bonus = estimate(origin, dest, START, config=PlannerConfig(air_bonus=20.0))
    assert high_bonus[0].primary_mode is Mode.FLIGHT
    # With no air bonus, ground (train) outranks flight on this short hop.
    assert low_bonus[0].primary_mode is not Mode.FLIGHT


def test_end_window_feasibility():
    origin = city("New York")
    dest = city("Tokyo")
    tight_end = START + timedelta(hours=2)
    results = estimate(origin, dest, START, end=tight_end)
    # Nothing crosses the Pacific in 2 hours; best is marked infeasible.
    assert results[0].feasible is False
    assert results[0].slack is not None and results[0].slack < timedelta()


def test_generous_window_is_feasible_with_slack():
    origin = city("New York")
    dest = city("Tokyo")
    generous_end = START + timedelta(days=2)
    results = estimate(origin, dest, START, end=generous_end)
    best = results[0]
    assert best.feasible is True
    assert best.slack is not None and best.slack > timedelta()


def test_airport_origin_skips_access_leg():
    origin = place("JFK", LocationType.AIRPORT, 40.6413, -73.7781)
    dest = city("Tokyo")
    results = estimate(origin, dest, START)
    flight_itin = next(it for it in results if it.primary_mode is Mode.FLIGHT)
    assert flight_itin.legs[0].mode is Mode.FLIGHT
