from datetime import timedelta

from travelplanner.models import CostLevel, LocationType, Mode
from travelplanner.modes import DEFAULT_PROFILES, PlannerConfig
from travelplanner.planner import _build_leg
from travelplanner import place


def test_cost_level_rank_roundtrip():
    for level in CostLevel:
        assert CostLevel.from_rank(level.rank) is level


def test_cost_level_from_rank_clamps():
    assert CostLevel.from_rank(0) is CostLevel.LOW
    assert CostLevel.from_rank(99) is CostLevel.HIGH


def test_build_leg_duration_math():
    config = PlannerConfig()
    a = place("A", LocationType.CITY, 0.0, 0.0)
    b = place("B", LocationType.CITY, 0.0, 1.0)
    leg = _build_leg(Mode.CAR, a, b, 130.0, config)
    # 130 km at 65 km/h = 2 hours travel + 5 min overhead.
    assert leg.travel_time == timedelta(hours=2)
    assert leg.overhead == timedelta(minutes=5)
    assert leg.duration == timedelta(hours=2, minutes=5)
    assert leg.cost_level is CostLevel.MEDIUM


def test_flight_profile_has_high_cost_and_overhead():
    flight = DEFAULT_PROFILES[Mode.FLIGHT]
    assert flight.cost_level is CostLevel.HIGH
    assert flight.overhead >= timedelta(hours=1)
