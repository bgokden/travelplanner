"""Runnable real-life scenarios for the travelplanner engine.

These are the examples used in docs/how-it-works.html, with the full setup
(origin, destination, timetable, connector) visible. Run them directly:

    python examples/scenarios.py

Times are naive local; all stops here are within one timezone (Central
European), so no cross-timezone conversion is involved.
"""

from datetime import datetime, timedelta

from travelplanner import place, plan, Objective
from travelplanner.models import CostLevel, LocationType, Mode
from travelplanner.graph.schema import NodeType
from travelplanner.graph.scheduled import Stop, Timetable, make_trip
from travelplanner.graph.validity import Validity
from travelplanner.graph.coupling import GeometricConnector


def zurich_berlin():
    """A long domestic-feeling hop with a flight, a 1-change train, and a drive.

    Returns (timetable, connector, origin, dest, departure).
    """
    tt = Timetable()
    tt.add_stop(Stop("ZRH_AIR", "Zurich Airport", 47.4647, 8.5492, NodeType.AIRPORT))
    tt.add_stop(Stop("BER_AIR", "Berlin Brandenburg", 52.3667, 13.5033, NodeType.AIRPORT))
    tt.add_stop(Stop("ZRH_HB", "Zurich HB", 47.3779, 8.5403))
    tt.add_stop(Stop("HALLE", "Halle (Saale) Hbf", 51.4776, 11.9874))
    tt.add_stop(Stop("BER_HB", "Berlin Hbf", 52.5251, 13.3694))
    tt.add_trip(make_trip("LX192", Mode.FLIGHT,
                          [("ZRH_AIR", "10:00", "10:00"), ("BER_AIR", "11:15", "11:15")],
                          cost_level=CostLevel.HIGH))
    tt.add_trip(make_trip("ICE-A", Mode.TRAIN,
                          [("ZRH_HB", "08:00", "08:00"), ("HALLE", "14:30", "14:30")]))
    tt.add_trip(make_trip("ICE-B", Mode.TRAIN,
                          [("HALLE", "14:50", "14:50"), ("BER_HB", "16:00", "16:00")]))
    conn = GeometricConnector(tt.stops)
    origin = place("Hotel Storchen, Zurich", LocationType.HOTEL, 47.3705, 8.5417)
    dest = place("Office in Berlin-Mitte", LocationType.HOTEL, 52.5200, 13.4050)
    return tt, conn, origin, dest, datetime(2026, 7, 1, 7, 30)


def dubrovnik_mljet():
    """An island reachable only by a summer-only ferry (no road across the sea).

    Uses the CCH road engine with a disconnected island component. Returns
    (timetable, connector, origin, dest, summer_departure, winter_departure).
    """
    from travelplanner.graph.road import CCHRoadRouter, RoadGraphBuilder
    from travelplanner.graph.coupling import CCHConnector

    rb = RoadGraphBuilder()
    rb.add_node("dbv_town", 42.6507, 18.0944)
    rb.add_node("dbv_port", 42.6600, 18.0700)
    rb.add_node("mlj_port", 42.7370, 17.6170)
    rb.add_node("mlj_village", 42.7900, 17.5400)
    rb.add_road("dbv_town", "dbv_port", 600)
    rb.add_road("mlj_port", "mlj_village", 900)        # island: separate component
    router = CCHRoadRouter(rb.build())

    tt = Timetable()
    tt.add_stop(Stop("DBV", "Dubrovnik Port", 42.6600, 18.0700, NodeType.FERRY_TERMINAL))
    tt.add_stop(Stop("MLJ", "Mljet (Sobra)", 42.7370, 17.6170, NodeType.FERRY_TERMINAL))
    tt.add_trip(make_trip("CAT-9", Mode.FERRY,
                          [("DBV", "16:00", "16:00"), ("MLJ", "17:00", "17:00")],
                          validity=Validity(open_months=frozenset({5, 6, 7, 8, 9, 10})),
                          cost_level=CostLevel.LOW))
    conn = CCHConnector(router, tt.stops,
                        stop_to_node={"DBV": "dbv_port", "MLJ": "mlj_port"})
    origin = place("Old Town apartment, Dubrovnik", LocationType.HOTEL, 42.6420, 18.1100)
    dest = place("Villa on Mljet", LocationType.HOTEL, 42.7950, 17.5350)
    return (tt, conn, origin, dest,
            datetime(2026, 7, 20, 14, 0), datetime(2026, 1, 20, 14, 0))


def location_kinds():
    """The same destination reached from different KINDS of start point.

    Origin/destination can be any LocationType (CITY, LANDMARK, STATION,
    AIRPORT, HOTEL). Routing is by coordinates and the type is a label; the
    visible effect is that starting at a station/airport gives a ~0 access hop.
    Returns (timetable, connector, dest, [origins], departure).
    """
    tt, conn, _origin, _dest, depart = zurich_berlin()
    dest = place("Berlin city centre", LocationType.CITY, 52.5200, 13.4050)
    origins = [
        place("Zurich city centre", LocationType.CITY, 47.3769, 8.5417),
        place("Grossmunster", LocationType.LANDMARK, 47.3700, 8.5443),
        place("Zurich HB", LocationType.STATION, 47.3779, 8.5403),
        place("Zurich Airport", LocationType.AIRPORT, 47.4647, 8.5492),
    ]
    return tt, conn, dest, origins, depart


def _show(title, results):
    print(f"\n{title}")
    if not results:
        print("  (no itinerary - the destination is unreachable)")
        return
    it = results[0]
    print(f"  {it.primary_mode.value} | arrive {it.arrive_at:%H:%M} | "
          f"cost {it.cost_level.value}")
    for leg in it.legs:
        print(f"    {leg.mode.value:6} {leg.from_loc.name} -> {leg.to_loc.name}")


if __name__ == "__main__":
    tt, conn, origin, dest, depart = zurich_berlin()
    _show("Zurich -> Berlin (AIR_PRIORITY):",
          plan(origin, dest, depart, tt, conn, objective=Objective.AIR_PRIORITY))
    _show("Zurich -> Berlin (CHEAPEST):",
          plan(origin, dest, depart, tt, conn, objective=Objective.CHEAPEST))

    tt, conn, origin, dest, summer, winter = dubrovnik_mljet()
    _show("Dubrovnik -> Mljet (July):", plan(origin, dest, summer, tt, conn))
    _show("Dubrovnik -> Mljet (January):", plan(origin, dest, winter, tt, conn))

    tt, conn, dest, origins, depart = location_kinds()
    print("\nSame trip from different kinds of start point (to Berlin centre):")
    for o in origins:
        it = plan(o, dest, depart, tt, conn, objective=Objective.AIR_PRIORITY)[0]
        access = it.legs[0]
        mins = round(access.travel_time.total_seconds() / 60)
        print(f"  {o.type.value:8} {o.name:18} -> first hop: "
              f"{access.mode.value} to {access.to_loc.name} ({mins} min), "
              f"then {it.primary_mode.value}, arrive {it.arrive_at:%H:%M}")
