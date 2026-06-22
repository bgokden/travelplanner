"""travelplanner.graph.scheduled: scheduled layer (Phase 2).

Timetable model + Connection Scan Algorithm for trains, ferries, and flights.
Pure stdlib (csv + datetime); no external dependencies.
"""

from travelplanner.graph.scheduled.csa import ConnectionScan, Journey, JourneyLeg
from travelplanner.graph.scheduled.gtfs import load_timetable
from travelplanner.graph.scheduled.model import (
    Connection,
    Footpath,
    Stop,
    StopTime,
    Timetable,
    Trip,
    make_trip,
    merge_timetables,
    parse_gtfs_time,
)

__all__ = [
    "ConnectionScan",
    "Journey",
    "JourneyLeg",
    "Timetable",
    "Stop",
    "StopTime",
    "Trip",
    "Footpath",
    "Connection",
    "make_trip",
    "merge_timetables",
    "parse_gtfs_time",
    "load_timetable",
]
