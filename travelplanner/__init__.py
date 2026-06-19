"""travelplanner: multimodal door-to-door travel planning that prioritizes air.

Two entry points:

- `plan(...)`  — the multimodal graph engine: ground access + scheduled
  line-haul (rail/ferry/flight) + egress, with seasonal/conditional edges and
  multi-criteria (time / cost / transfers) selection, air prioritized. Needs a
  Timetable and a RoadConnector (build your own, or use the bundled sample).
- `estimate(...)` — a zero-dependency heuristic for a quick door-to-door guess
  from just two locations and a time (bundled airport/city tables).
"""

from travelplanner.catalog import resolve_city
from travelplanner.models import (
    CostLevel,
    Itinerary,
    Leg,
    Location,
    LocationType,
    Mode,
)
from travelplanner.modes import DEFAULT_PROFILES, ModeProfile, PlannerConfig
from travelplanner.planner import plan as estimate
from travelplanner.graph.query import Objective, TravelQuery
from travelplanner.graph.scheduled import (
    Stop,
    Timetable,
    load_timetable,
    make_trip,
)
from travelplanner.graph.coupling import (
    CCHConnector,
    GeometricConnector,
    RoadConnector,
    plan,
)
from travelplanner.samples import sample_timetable, sample_trip

__all__ = [
    # entry points
    "plan",
    "estimate",
    # location helpers
    "place",
    "city",
    "Location",
    "LocationType",
    "Mode",
    "CostLevel",
    "Leg",
    "Itinerary",
    # multimodal engine
    "Objective",
    "TravelQuery",
    "Timetable",
    "Stop",
    "make_trip",
    "load_timetable",
    "RoadConnector",
    "GeometricConnector",
    "CCHConnector",
    "sample_timetable",
    "sample_trip",
    # heuristic estimator config
    "PlannerConfig",
    "ModeProfile",
    "DEFAULT_PROFILES",
]

__version__ = "0.1.0"


def place(name: str, type: LocationType, lat: float, lon: float) -> Location:
    """Build a Location from an explicit coordinate."""
    return Location(name=name, type=type, lat=lat, lon=lon)


def city(name: str) -> Location:
    """Build a CITY Location resolved from the bundled city table."""
    lat, lon = resolve_city(name)
    return Location(name=name, type=LocationType.CITY, lat=lat, lon=lon)
