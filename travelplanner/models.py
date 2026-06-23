"""Core data types for the travel planner."""

import json
from dataclasses import dataclass
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
    """Relative trip-cost band, not a currency amount.

    The planner has no price model; this is an ordinal signal for comparing
    options within a result set (CHEAPEST ranks by it), like the $/$$/$$$ bands a
    maps app shows when it has no fare -- not an absolute price across all trips.
    """

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


# Line-haul (vehicle) modes; ground modes (walk/car) are access/egress only and
# do not count as transfers.
LINE_HAUL_MODES = frozenset({Mode.TRAIN, Mode.FERRY, Mode.FLIGHT})

# How each mode reads as a route-card step verb.
_MODE_VERB = {Mode.WALK: "Walk", Mode.CAR: "Drive", Mode.TRAIN: "Train",
              Mode.FERRY: "Ferry", Mode.FLIGHT: "Flight"}


def humanize_duration(td: timedelta) -> str:
    """A short human duration like "2h 9m" / "45m" / "1d 3h" (seconds dropped).

    For route-card display: a raw timedelta prints as "2:09:26.112926", which is
    not how a traveler reads a trip length.
    """
    minutes = max(0, int(td.total_seconds()) // 60)
    hours, mins = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if mins or not parts:                  # always show something, even "0m"
        parts.append(f"{mins}m")
    return " ".join(parts)


def _iso_seconds(dt: datetime) -> str:
    """ISO string at whole-second resolution. Summing per-leg float seconds yields
    spurious microseconds (e.g. 10:09:26.112926); a clock time has none."""
    return dt.replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class Location:
    name: str
    type: LocationType
    lat: float
    lon: float
    # IANA timezone for displaying this place's local clock (e.g. an arrival
    # airport's zone). None means "show in the trip's reference zone"; it is set
    # on itinerary leg endpoints so each leg can render in its own local time.
    tz: str | None = None

    def __post_init__(self) -> None:
        if not -90.0 <= self.lat <= 90.0:
            raise ValueError(
                f"latitude {self.lat} out of range [-90, 90] for {self.name!r}")
        if not -180.0 <= self.lon <= 180.0:
            raise ValueError(
                f"longitude {self.lon} out of range [-180, 180] for {self.name!r}")

    def to_dict(self) -> dict:
        out = {"name": self.name, "type": self.type.value,
               "lat": self.lat, "lon": self.lon}
        if self.tz:
            out["tz"] = self.tz
        return out


@dataclass
class Leg:
    mode: Mode
    from_loc: Location
    to_loc: Location
    distance_km: float
    travel_time: timedelta
    overhead: timedelta
    cost_level: CostLevel
    # Absolute clock times for this leg, stamped by the owning Itinerary (None for
    # a free-standing leg). depart_at is when the leg's vehicle/walk leaves (after
    # any wait counted in overhead); arrive_at is when it reaches to_loc. Render
    # each in its endpoint's local zone (from_loc.tz / to_loc.tz).
    depart_at: datetime | None = None
    arrive_at: datetime | None = None

    @property
    def duration(self) -> timedelta:
        return self.travel_time + self.overhead

    def describe(self) -> str:
        """A one-line route-card step, e.g. "Walk to Schiphol" or "Flight from
        Schiphol to Zurich Airport" (line-haul names both ends)."""
        verb = _MODE_VERB.get(self.mode, self.mode.value.title())
        if self.mode in LINE_HAUL_MODES:
            return f"{verb} from {self.from_loc.name} to {self.to_loc.name}"
        return f"{verb} to {self.to_loc.name}"

    def to_dict(self) -> dict:
        """JSON-safe dict (enums -> value, durations -> seconds)."""
        out = {
            "mode": self.mode.value,
            "summary": self.describe(),
            "from": self.from_loc.to_dict(),
            "to": self.to_loc.to_dict(),
            "distance_km": self.distance_km,
            "travel_time_s": self.travel_time.total_seconds(),
            "overhead_s": self.overhead.total_seconds(),
            "duration_s": self.duration.total_seconds(),
            "duration_human": humanize_duration(self.duration),
            "cost_level": self.cost_level.value,
        }
        if self.depart_at is not None:
            out["depart_at"] = _iso_seconds(self.depart_at)
        if self.arrive_at is not None:
            out["arrive_at"] = _iso_seconds(self.arrive_at)
        return out


@dataclass
class Itinerary:
    legs: list[Leg]
    depart_at: datetime
    score: float
    feasible: bool = True
    slack: timedelta | None = None
    arrival_window_end: datetime | None = None

    def __post_init__(self) -> None:
        # Stamp each leg's absolute clock times from the departure, so a consumer
        # can show "09:00 -> 10:30" per step without re-deriving the running clock.
        # overhead is the wait before the leg; travel_time is the move itself.
        clock = self.depart_at
        for leg in self.legs:
            clock = clock + leg.overhead
            leg.depart_at = clock
            clock = clock + leg.travel_time
            leg.arrive_at = clock

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

    @property
    def total_minutes(self) -> float:
        return self.total_duration.total_seconds() / 60.0

    @property
    def num_transfers(self) -> int:
        """Vehicle changes: line-haul legs minus 1 (ground access/egress excluded)."""
        line_haul = sum(1 for leg in self.legs if leg.mode in LINE_HAUL_MODES)
        return max(0, line_haul - 1)

    def to_dict(self, *, with_legs: bool = True) -> dict:
        """JSON-safe dict of the itinerary (enums -> value, datetimes -> ISO)."""
        out = {
            "primary_mode": self.primary_mode.value,
            "depart_at": _iso_seconds(self.depart_at),
            "arrive_at": _iso_seconds(self.arrive_at),
            "total_duration_s": self.total_duration.total_seconds(),
            "total_duration_human": humanize_duration(self.total_duration),
            "total_minutes": self.total_minutes,
            "total_distance_km": self.total_distance_km,
            "cost_level": self.cost_level.value,
            "num_transfers": self.num_transfers,
            "score": self.score,
            "feasible": self.feasible,
            "slack_s": self.slack.total_seconds() if self.slack is not None else None,
            "arrival_window_end": (_iso_seconds(self.arrival_window_end)
                                   if self.arrival_window_end is not None else None),
        }
        if with_legs:
            out["legs"] = [leg.to_dict() for leg in self.legs]
        return out

    def to_json(self, *, with_legs: bool = True) -> str:
        return json.dumps(self.to_dict(with_legs=with_legs))


def itinerary_records(itineraries: list[Itinerary]) -> list[dict]:
    """One JSON-safe row per itinerary (no nested legs), tagged with rank.

    Ready for `pandas.DataFrame(itinerary_records(results))` without importing
    pandas here.
    """
    return [{"rank": i, **it.to_dict(with_legs=False)}
            for i, it in enumerate(itineraries)]


def leg_records(itineraries: list[Itinerary]) -> list[dict]:
    """One row per leg across all itineraries, tagged with itinerary rank/index."""
    rows = []
    for i, it in enumerate(itineraries):
        for j, leg in enumerate(it.legs):
            rows.append({"itinerary": i, "leg_index": j, **leg.to_dict()})
    return rows
