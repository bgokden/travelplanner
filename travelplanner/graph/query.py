"""Query input contract for the v2 multimodal planner.

Output reuses the v1 models (Itinerary / Leg / CostLevel) so the result shape
is stable across engines.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from travelplanner.models import Location


class Objective(Enum):
    FASTEST = "fastest"
    CHEAPEST = "cheapest"
    FEWEST_TRANSFERS = "fewest_transfers"
    AIR_PRIORITY = "air_priority"


@dataclass(frozen=True)
class TravelQuery:
    origin: Location
    dest: Location
    depart_after: datetime
    arrive_before: datetime | None = None
    conditions: frozenset[str] = field(default_factory=frozenset)
    objective: Objective = Objective.AIR_PRIORITY
    top_n: int = 3

    def __post_init__(self) -> None:
        if self.arrive_before is not None and self.arrive_before <= self.depart_after:
            raise ValueError("arrive_before must be after depart_after")
        if self.top_n < 1:
            raise ValueError("top_n must be >= 1")
