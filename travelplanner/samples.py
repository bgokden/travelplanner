"""Bundled sample data so the multimodal engine works out of the box.

`sample_timetable()` returns a small scenario with both a fast flight and a
slower, cheaper multi-leg train between two regions, plus a seasonal lake
ferry. It is enough to exercise the door-to-door planner and the different
objectives without any external GTFS/OSM data.
"""

from datetime import datetime

from travelplanner.models import CostLevel, Location, LocationType, Mode
from travelplanner.graph.schema import NodeType
from travelplanner.graph.scheduled.model import Stop, Timetable, make_trip
from travelplanner.graph.validity import Validity


def sample_timetable() -> Timetable:
    tt = Timetable()
    tt.add_stop(Stop("X_AIR", "Westport Airport", 47.00, 7.00, NodeType.AIRPORT))
    tt.add_stop(Stop("Y_AIR", "Eastport Airport", 45.00, 9.00, NodeType.AIRPORT))
    tt.add_stop(Stop("X_RAIL", "Westport Station", 47.00, 7.02))
    tt.add_stop(Stop("MID", "Midvale Station", 46.00, 8.00))
    tt.add_stop(Stop("Y_RAIL", "Eastport Station", 45.00, 9.02))
    tt.add_stop(Stop("Y_PIER", "Eastport Pier", 45.01, 9.03,
                     NodeType.FERRY_TERMINAL))
    tt.add_stop(Stop("ISLE_PIER", "Isle Pier", 45.05, 9.10,
                     NodeType.FERRY_TERMINAL))

    # Fast but pricey flight (0 transfers).
    tt.add_trip(make_trip("FL100", Mode.FLIGHT, [
        ("X_AIR", "09:00", "09:00"), ("Y_AIR", "10:00", "10:00")],
        cost_level=CostLevel.HIGH))
    # Slower, cheaper train with one change.
    tt.add_trip(make_trip("IC1", Mode.TRAIN, [
        ("X_RAIL", "09:00", "09:00"), ("MID", "11:00", "11:00")]))
    tt.add_trip(make_trip("IC2", Mode.TRAIN, [
        ("MID", "11:10", "11:10"), ("Y_RAIL", "13:00", "13:00")]))
    # Seasonal lake ferry (summer only).
    tt.add_trip(make_trip("FERRY9", Mode.FERRY, [
        ("Y_PIER", "13:30", "13:30"), ("ISLE_PIER", "14:00", "14:00")],
        validity=Validity(open_months=frozenset({6, 7, 8, 9})),
        cost_level=CostLevel.LOW))
    return tt


def sample_trip() -> tuple[Location, Location, datetime]:
    """A sample (origin, destination, departure) matching sample_timetable()."""
    origin = Location("Hotel Westport", LocationType.HOTEL, 47.00, 7.005)
    dest = Location("Eastport Hotel", LocationType.HOTEL, 45.00, 9.01)
    return origin, dest, datetime(2026, 7, 1, 8, 0)
