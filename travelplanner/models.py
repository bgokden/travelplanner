"""Core data types for the travel planner."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum


class LocationType(Enum):
    HOTEL = "hotel"
    CITY = "city"
    LANDMARK = "landmark"
    STATION = "station"
    AIRPORT = "airport"


class Mode(Enum):
    WALK = "walk"
    CAR = "car"
    TRAIN = "train"
    FERRY = "ferry"
    FLIGHT = "flight"


class CostLevel(Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    @property
    def rank(self) -> int:
        return {"low": 1, "medium": 2, "high": 3}[self.value]

    @classmethod
    def from_rank(cls, rank: int) -> "CostLevel":
        clamped = max(1, min(3, rank))
        return {1: cls.LOW, 2: cls.MEDIUM, 3: cls.HIGH}[clamped]


@dataclass(frozen=True)
class Location:
    name: str
    type: LocationType
    lat: float
    lon: float


@dataclass
class Leg:
    mode: Mode
    from_loc: Location
    to_loc: Location
    distance_km: float
    travel_time: timedelta
    overhead: timedelta
    cost_level: CostLevel

    @property
    def duration(self) -> timedelta:
        return self.travel_time + self.overhead


@dataclass
class Itinerary:
    legs: list[Leg]
    depart_at: datetime
    score: float
    feasible: bool = True
    slack: timedelta | None = None
    arrival_window_end: datetime | None = None

    @property
    def total_duration(self) -> timedelta:
        return sum((leg.duration for leg in self.legs), timedelta())

    @property
    def arrive_at(self) -> datetime:
        return self.depart_at + self.total_duration

    @property
    def total_distance_km(self) -> float:
        return sum(leg.distance_km for leg in self.legs)

    @property
    def primary_mode(self) -> Mode:
        return max(self.legs, key=lambda leg: leg.distance_km).mode

    @property
    def cost_level(self) -> CostLevel:
        return CostLevel.from_rank(max(leg.cost_level.rank for leg in self.legs))
