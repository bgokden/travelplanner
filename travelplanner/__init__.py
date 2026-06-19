"""travelplanner: multimodal door-to-door travel planning that prioritizes air.

`plan(...)` is the graph engine: ground access + scheduled line-haul
(rail/ferry/flight) + egress, with seasonal/conditional edges and multi-criteria
(time / cost / transfers) selection, air prioritized. Because it only traverses
edges that exist in the graph, it never proposes a route that isn't real.

It routes over the data you give it: a Timetable (build one, or `load_timetable`
a GTFS feed) and a RoadConnector. A bundled `sample_timetable()` lets you run
immediately.
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
from travelplanner.roads import (
    DriveResult,
    REGIONS,
    build_region,
    download_region,
    drive,
    prefetch,
    region_connector,
    road_router,
)

__all__ = [
    "plan",
    # location helpers
    "place",
    "city",
    "Location",
    "LocationType",
    "Mode",
    "CostLevel",
    "Leg",
    "Itinerary",
    # engine
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
    # street-accurate driving (on-demand OSM)
    "drive",
    "DriveResult",
    "road_router",
    "region_connector",
    "build_region",
    "download_region",
    "prefetch",
    "REGIONS",
]

__version__ = "0.1.0"


def place(name: str, type: LocationType, lat: float, lon: float) -> Location:
    """Build a Location from an explicit coordinate."""
    return Location(name=name, type=type, lat=lat, lon=lon)


def city(name: str) -> Location:
    """Build a CITY Location resolved from the bundled city table."""
    lat, lon = resolve_city(name)
    return Location(name=name, type=LocationType.CITY, lat=lat, lon=lon)
