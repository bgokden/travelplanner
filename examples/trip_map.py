"""Render a sample door-to-door trip as a coloured overlay on the map.

Builds a multimodal itinerary with `plan_trip` (car access -> flight -> car
egress) and writes a self-contained Leaflet HTML map where each leg is a
coloured segment (walk grey, car blue, train green, ferry teal, flight orange).
No network and no road data required -- legs are drawn straight between their
endpoints. Run it:

    python examples/trip_map.py        # writes trip_map.html

For an accurate overlay where the road legs follow the streets, compute each
ground leg's geometry with `drive_route` (downloads an OSM extract for the region
on first use) and pass it via `geometries=` -- see the bottom of this file.
"""

from datetime import datetime

from travelplanner import plan_trip, place
from travelplanner.models import CostLevel, LocationType, Mode
from travelplanner.graph.schema import NodeType
from travelplanner.graph.scheduled import Stop, Timetable, make_trip
from travelplanner.viz import save_itinerary_map


def sample_door_to_door():
    """A flight trip Amsterdam -> Vaduz: drive to Schiphol, fly, drive to the
    door. Returns (timetable, origin, dest, departure)."""
    tt = Timetable()
    tt.add_stop(Stop("SPL", "Amsterdam Schiphol", 52.3105, 4.7683, NodeType.AIRPORT))
    tt.add_stop(Stop("VAD", "Vaduz Airfield", 47.140, 9.510, NodeType.AIRPORT))
    tt.add_trip(make_trip("LX-FLT", Mode.FLIGHT,
                          [("SPL", "10:00", "10:00"), ("VAD", "11:30", "11:30")],
                          cost_level=CostLevel.HIGH))
    origin = place("Amsterdam centre", LocationType.HOTEL, 52.3702, 4.8952)
    dest = place("Vaduz town", LocationType.HOTEL, 47.166, 9.523)
    return tt, origin, dest, datetime(2026, 7, 1, 7, 30)


if __name__ == "__main__":
    tt, origin, dest, depart = sample_door_to_door()
    itinerary = plan_trip(origin, dest, depart, tt)[0]
    print("itinerary:", " -> ".join(leg.mode.value for leg in itinerary.legs),
          f"| arrive {itinerary.arrive_at:%H:%M}")

    save_itinerary_map(itinerary, "trip_map.html",
                       title="Sample trip: Amsterdam -> Vaduz (door-to-door)")
    print("wrote trip_map.html (open it in a browser)")

    # Accurate road overlay (optional; downloads an OSM extract on first use).
    # Route each ground leg and map it by its 1-based leg index; the region is
    # auto-selected from the coordinates when omitted (here both ends resolve to
    # their own country extract):
    #
    #     from travelplanner import drive_route
    #     access = drive_route(origin, (52.3105, 4.7683))   # Amsterdam -> Schiphol
    #     egress = drive_route((47.140, 9.510), dest)       # Vaduz Airfield -> town
    #     geometries = {1: access.geometry, 3: egress.geometry}  # leg idx -> path
    #     save_itinerary_map(itinerary, "trip_map.html", geometries=geometries)
