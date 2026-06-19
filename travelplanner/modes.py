"""Per-mode travel profiles and planner configuration."""

from dataclasses import dataclass, field
from datetime import timedelta

from travelplanner.models import CostLevel, Mode


@dataclass(frozen=True)
class ModeProfile:
    avg_speed_kmh: float
    overhead: timedelta
    cost_level: CostLevel
    min_distance_km: float = 0.0
    max_distance_km: float = float("inf")


DEFAULT_PROFILES: dict[Mode, ModeProfile] = {
    Mode.WALK: ModeProfile(
        avg_speed_kmh=5.0,
        overhead=timedelta(0),
        cost_level=CostLevel.LOW,
        max_distance_km=3.0,
    ),
    Mode.CAR: ModeProfile(
        avg_speed_kmh=65.0,
        overhead=timedelta(minutes=5),
        cost_level=CostLevel.MEDIUM,
    ),
    Mode.TRAIN: ModeProfile(
        avg_speed_kmh=120.0,
        overhead=timedelta(minutes=20),
        cost_level=CostLevel.MEDIUM,
        min_distance_km=20.0,
    ),
    Mode.FLIGHT: ModeProfile(
        avg_speed_kmh=750.0,
        overhead=timedelta(minutes=120),
        cost_level=CostLevel.HIGH,
        min_distance_km=300.0,
    ),
}


@dataclass
class PlannerConfig:
    """Tunable weights and models for scoring itineraries.

    Score = w_time * total_hours + w_cost * total_cost_rank - air_bonus(if air).
    Lower score is better.
    """

    profiles: dict[Mode, ModeProfile] = field(
        default_factory=lambda: dict(DEFAULT_PROFILES)
    )
    detour_factor: float = 1.3
    w_time: float = 1.0
    w_cost: float = 0.5
    air_bonus: float = 4.0
    min_air_distance_km: float = 300.0
    top_n: int = 3
