"""Runnable real-life scenarios for the travelplanner engine.

These are the examples used in docs/how-it-works.md, with the full setup
(origin, destination, timetable, connector) visible. Run them directly:

    uv run python examples/scenarios.py

Times are naive local; all stops here are within one timezone (Central
European), so no cross-timezone conversion is involved.
"""

import warnings
from datetime import datetime
from urllib.error import URLError

from travelplanner import place, plan, plan_trip, Objective
from travelplanner.trips import plan_trip_choices, preference_kwargs
from travelplanner.samples import sample_timetable, sample_trip
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


def airport_access():
    """Door-to-door with plan_trip and a choice of first/last mile.

    A local train (Amsterdam Centraal -> Schiphol) feeds an onward flight. With
    car access plan_trip drives to the airport; with access="transit" it walks to
    the station and takes the train. Returns (timetable, origin, dest, departure).
    """
    tt = Timetable()
    tt.add_stop(Stop("ASD_CS", "Amsterdam Centraal", 52.3791, 4.9003, NodeType.RAIL_STATION))
    tt.add_stop(Stop("SPL", "Schiphol", 52.3105, 4.7683, NodeType.AIRPORT))
    tt.add_stop(Stop("VAD", "Vaduz Airfield", 47.140, 9.510, NodeType.AIRPORT))
    tt.add_trip(make_trip("IC-DIRECT", Mode.TRAIN,
                          [("ASD_CS", "09:00", "09:00"), ("SPL", "09:16", "09:16")]))
    tt.add_trip(make_trip("LX-FLT", Mode.FLIGHT,
                          [("SPL", "10:00", "10:00"), ("VAD", "11:30", "11:30")],
                          cost_level=CostLevel.HIGH))
    origin = place("Amsterdam centre", LocationType.HOTEL, 52.3702, 4.8952)
    dest = place("Vaduz, Liechtenstein", LocationType.HOTEL, 47.1410, 9.5215)
    return tt, origin, dest, datetime(2026, 7, 1, 7, 30)


def rush_hour_driving():
    """The same drive at different departure times: with road=True the car legs are
    time-of-day aware (a weekday rush hour is slower than the night).

    A classified ("primary") road, so the speed model applies a congestion
    multiplier keyed on the departure. Returns (timetable, connector, origin, dest).
    """
    from travelplanner.graph.road import CCHRoadRouter, RoadGraphBuilder
    from travelplanner.graph.coupling import CCHConnector

    rb = RoadGraphBuilder()
    rb.add_node("a", 47.00, 8.00)
    rb.add_node("b", 47.00, 8.40)                       # ~30 km east
    rb.add_road("a", "b", 1200, highway="primary")
    router = CCHRoadRouter(rb.build())
    tt = Timetable()                                    # no stops: the drive is the trip
    conn = CCHConnector(router, tt.stops)
    origin = place("home", LocationType.HOTEL, 47.00, 8.00)
    dest = place("office", LocationType.HOTEL, 47.00, 8.40)
    return tt, conn, origin, dest


def _show(title, results):
    print(f"\n{title}")
    if not results:
        print("  (no itinerary - the destination is unreachable)")
        return
    it = results[0]
    fare = (f" | ~{it.fare_estimate:.0f} {it.fare_currency}"
            if it.fare_estimate is not None else "")
    print(f"  {it.primary_mode.value} | {it.total_duration_human} | "
          f"arrive {it.arrive_at:%H:%M}{fare} | cost {it.cost_level.value}")
    for leg in it.legs:
        print(f"    {leg.depart_at:%H:%M}-{leg.arrive_at:%H:%M}  {leg.describe()}")


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

    tt, origin, dest, depart = airport_access()
    print("\nplan_trip door-to-door, choosing the first/last mile:")
    for access in ("car", "transit"):
        it = plan_trip(origin, dest, depart, tt, access=access)[0]
        chain = " -> ".join(leg.mode.value for leg in it.legs)
        print(f"  access={access:8} {chain}, arrive {it.arrive_at:%H:%M}")

    # A transport preference (default: public transit) plus the labelled, by-purpose
    # choices the demo shows -- one best option per objective, deduped. The sample
    # feed offers a fast pricey flight and a cheap slower train, both walk-reachable.
    s_origin, s_dest, s_depart = sample_trip()
    objectives = [(Objective.FASTEST, "Fastest"), (Objective.CHEAPEST, "Cheapest"),
                  (Objective.GREENEST, "Greenest"),
                  (Objective.FEWEST_TRANSFERS, "Fewest changes")]
    print("\nplan_trip_choices labelled by purpose (preference: public transit):")
    for itinerary, labels in plan_trip_choices(s_origin, s_dest, s_depart,
                                               sample_timetable(),
                                               objectives=objectives,
                                               **preference_kwargs("transit")):
        chain = " -> ".join(leg.mode.value for leg in itinerary.legs)
        print(f"  [{', '.join(labels)}] {itinerary.total_duration_human}: {chain}")

    tt, conn, origin, dest = rush_hour_driving()
    print("\nTime-of-day driving (road=True over a classified road):")
    for label, hour in [("rush 08:00", 8), ("off-peak 13:00", 13), ("night 03:00", 3)]:
        it = plan(origin, dest, datetime(2026, 7, 1, hour, 0), tt, conn)[0]
        print(f"  depart {label:14} -> drive {it.total_duration_human}")

    # No timetable at all: plan_trip auto-composes one for the trip (flights +
    # GTFS by location). Unlike the scenarios above, this needs network on first
    # run to download the catalog/feeds, so it degrades gracefully when offline.
    print("\nplan_trip with NO timetable (auto-sourced flights + GTFS):")
    ams = place("Amsterdam", LocationType.CITY, 52.3791, 4.9003)
    zrh = place("Zurich", LocationType.CITY, 47.3769, 8.5417)
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            results = plan_trip(ams, zrh, datetime(2026, 7, 1, 8, 0),
                                objective=Objective.AIR_PRIORITY)
        for w in caught:
            print(f"  note: {w.message}")
        _show("  Amsterdam -> Zurich (auto):", results)
    except (URLError, OSError) as exc:
        print(f"  (skipped - no network to fetch catalog/feeds: {exc})")
