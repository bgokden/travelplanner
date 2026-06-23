"""travelplanner: multimodal door-to-door travel planning that prioritizes air.

`plan(...)` is the graph engine: ground access + scheduled line-haul
(rail/ferry/flight) + egress, with seasonal/conditional edges and multi-criteria
(time / cost / transfers) selection, air prioritized. Because it only traverses
edges that exist in the graph, it never proposes a route that isn't real.

It routes over the data you give it: a Timetable (build one, or `load_timetable`
a GTFS feed) and a RoadConnector. A bundled `sample_timetable()` lets you run
immediately.
"""

from travelplanner.geocoding import (
    geocode,
    reset_geocoder,
    resolve_city,
    set_geocoder,
)
from travelplanner.models import (
    CostLevel,
    Itinerary,
    Leg,
    Location,
    LocationType,
    Mode,
    itinerary_records,
    leg_records,
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
    SplitConnector,
    plan,
    plan_multi,
)
from travelplanner.trips import plan_trip
from travelplanner.openflights import load_openflights
from travelplanner.samples import sample_timetable, sample_trip
from travelplanner.roads import (
    DriveResult,
    REGIONS,
    build_region,
    continent_road,
    download_region,
    Route,
    drive,
    drive_matrix,
    drive_route,
    prefetch,
    region_connector,
    road_router,
    set_continent_road,
)
from travelplanner.geofabrik import (
    Region,
    catalog,
    list_regions,
    region_for,
    region_for_trip,
)
from travelplanner.fares import (
    fare_currency,
    free_model,
    get_fare_model,
    heuristic_fare_model,
    reset_fare_model,
    set_fare_model,
)
from travelplanner.speed import (
    average_model,
    free_flow_model,
    get_speed_model,
    holiday_calendar,
    reset_speed_model,
    set_speed_model,
    time_of_day_model,
)

__all__ = [
    "plan",
    "plan_multi",
    "plan_trip",
    # location helpers
    "place",
    "city",
    "Location",
    "LocationType",
    "Mode",
    "CostLevel",
    "Leg",
    "Itinerary",
    "itinerary_records",
    "leg_records",
    # engine
    "Objective",
    "TravelQuery",
    "Timetable",
    "Stop",
    "make_trip",
    "load_timetable",
    "load_openflights",
    "RoadConnector",
    "GeometricConnector",
    "CCHConnector",
    "SplitConnector",
    "sample_timetable",
    "sample_trip",
    # street-accurate driving (on-demand OSM)
    "drive",
    "drive_matrix",
    "drive_route",
    "Route",
    "DriveResult",
    "road_router",
    "region_connector",
    "build_region",
    "download_region",
    "prefetch",
    "set_continent_road",
    "continent_road",
    "REGIONS",
    # region catalog (Geofabrik)
    "list_regions",
    "catalog",
    "Region",
    "region_for",
    "region_for_trip",
    # geocoding (name -> lat/lon); composition helpers in travelplanner.geocoding
    "geocode",
    "set_geocoder",
    "reset_geocoder",
    # driving speed models (free-flow / average / time-of-day)
    "set_speed_model",
    "get_speed_model",
    "reset_speed_model",
    "free_flow_model",
    "average_model",
    "time_of_day_model",
    "holiday_calendar",
    # approximate fare models (distance-and-mode heuristic; estimate, not a quote)
    "set_fare_model",
    "get_fare_model",
    "reset_fare_model",
    "heuristic_fare_model",
    "free_model",
    "fare_currency",
]

__version__ = "0.1.0"


def place(name: str, type: LocationType, lat: float, lon: float) -> Location:
    """Build a Location from an explicit coordinate."""
    return Location(name=name, type=type, lat=lat, lon=lon)


def city(name: str, *, geocoder=None) -> Location:
    """Build a CITY Location by resolving a name via the active/given geocoder."""
    lat, lon = resolve_city(name, geocoder=geocoder)
    return Location(name=name, type=LocationType.CITY, lat=lat, lon=lon)
