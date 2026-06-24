"""Door-to-door coupling tests (Phase 3)."""

from datetime import date, datetime, timedelta

from travelplanner import place
from travelplanner.fares import (
    FARE_RATES, heuristic_fare_model, reset_fare_model, set_fare_model)
from travelplanner.models import CostLevel, LocationType, Mode
from travelplanner.graph.schema import NodeType
from travelplanner.graph.scheduled import Stop, Timetable, make_trip
from travelplanner.graph.coupling import GeometricConnector, plan
from travelplanner.graph.validity import Validity
from travelplanner.graph.road import CCHRoadRouter, RoadGraphBuilder
from travelplanner.graph.coupling import CCHConnector

DEP = datetime(2026, 7, 1, 8, 0)


def _stop(sid, lat, lon, ntype=NodeType.RAIL_STATION):
    return Stop(id=sid, name=sid, lat=lat, lon=lon, type=ntype)


def _has_mode(itineraries, mode):
    return any(leg.mode is mode for it in itineraries for leg in it.legs)


def test_door_to_door_train_beats_driving():
    tt = Timetable()
    tt.add_stop(_stop("StA", 47.0, 7.0))
    tt.add_stop(_stop("StC", 44.0, 9.0))        # ~360 km away: fast rail beats a drive
    tt.add_trip(make_trip("T", Mode.TRAIN, [
        ("StA", "09:00", "09:00"), ("StC", "10:00", "10:00")]))
    conn = GeometricConnector(tt.stops)
    origin = place("HomeA", LocationType.HOTEL, 47.01, 7.01)
    dest = place("HotelC", LocationType.HOTEL, 43.99, 8.99)

    results = plan(origin, dest, DEP, tt, conn)
    assert results
    top = results[0]
    assert top.primary_mode is Mode.TRAIN
    # The in-vehicle train leg is exactly one hour.
    train_legs = [leg for leg in top.legs if leg.mode is Mode.TRAIN]
    assert len(train_legs) == 1
    assert train_legs[0].travel_time == timedelta(hours=1)
    # Total duration equals door-to-door elapsed (waits folded into overhead).
    assert top.arrive_at == top.depart_at + top.total_duration
    # Waiting at the station shows up as overhead somewhere.
    assert any(leg.overhead > timedelta() for leg in top.legs)
    # The slower pure-drive option is strictly dominated, so the Pareto frontier
    # drops it.
    assert not any(len(it.legs) == 1 and it.primary_mode is Mode.CAR
                   for it in results)


def test_drive_time_is_monotonic_in_distance():
    """Regression: a step speed function chosen by total distance made a leg just
    past a band boundary (road > 150 km -> 100 km/h) FASTER than a shorter one
    (road <= 150 km -> 80 km/h). Marginal bracket speeds make drive time strictly
    increasing in distance with no boundary inversion."""
    from travelplanner.graph.coupling.connector import _drive_seconds
    secs = [_drive_seconds(float(km), 60.0) for km in range(0, 400)]
    assert all(b > a for a, b in zip(secs, secs[1:]))           # strictly increasing
    assert _drive_seconds(156.0, 60.0) > _drive_seconds(149.5, 60.0)  # no inversion


def test_geometric_direct_drive_time_monotonic_across_band():
    # Public-path regression (fails on the old step model): a farther destination
    # must never be reported as a shorter drive. The step model made ~120 km
    # faster than ~115 km at the 150 km road band boundary.
    conn = GeometricConnector({})
    origin = place("o", LocationType.HOTEL, 47.0, 7.0)
    times = []
    for dlat in range(50, 250, 5):              # 0.50..2.45 deg N -> spans the band
        dest = place("d", LocationType.HOTEL, 47.0 + dlat / 100.0, 7.0)
        leg = conn.direct(origin, dest)
        assert leg is not None and leg.mode is Mode.CAR
        times.append(leg.seconds)
    assert times == sorted(times)              # non-decreasing with distance


def test_plan_skips_phantom_stop_journey():
    # A journey that rides through a stop with no Stop entry (a dangling footpath/
    # trip reference) is routed around, not crashed-on with an opaque KeyError.
    tt = Timetable()
    tt.add_stop(_stop("S", 47.0, 7.0))
    tt.add_stop(_stop("D", 46.0, 8.0))
    tt.add_footpath("S", "GHOST", timedelta(minutes=5))
    tt.add_trip(make_trip("TR", Mode.TRAIN, [
        ("GHOST", "09:00", "09:00"), ("D", "10:00", "10:00")]))
    conn = GeometricConnector(tt.stops)
    origin = place("o", LocationType.HOTEL, 47.0, 7.0)
    dest = place("d", LocationType.HOTEL, 46.0, 8.0)
    results = plan(origin, dest, DEP, tt, conn)                # no crash
    assert all(leg.mode is not Mode.TRAIN
               for it in results for leg in it.legs)           # phantom journey skipped


def test_plan_tolerates_unregistered_interior_stop():
    # The scheduled layer routes around an unregistered INTERIOR stop (CSA skips
    # interior stops), so the feed must still plan -- not be rejected wholesale.
    tt = Timetable()
    tt.add_stop(_stop("A", 47.0, 7.0))
    tt.add_stop(_stop("C", 45.0, 9.0))
    tt.add_trip(make_trip("T", Mode.TRAIN, [
        ("A", "09:00", "09:00"), ("B", "09:30", "09:30"), ("C", "11:00", "11:00")]))
    conn = GeometricConnector(tt.stops)
    origin = place("o", LocationType.HOTEL, 47.0, 7.0)
    dest = place("d", LocationType.HOTEL, 45.0, 9.0)
    results = plan(origin, dest, DEP, tt, conn)                # no raise
    assert any(leg.mode is Mode.TRAIN for it in results for leg in it.legs)


def test_geometric_connector_refuses_transoceanic_ground():
    # New York -> Tokyo straight line is ~10,800 km; the geometric connector
    # must not offer a pure-ground "drive across the ocean" candidate.
    conn = GeometricConnector({})
    ny = place("New York", LocationType.CITY, 40.71, -74.01)
    tokyo = place("Tokyo", LocationType.CITY, 35.68, 139.69)
    assert conn.direct(ny, tokyo, frozenset()) is None
    # A plausible regional distance still gets a ground leg.
    nearby = place("Philadelphia", LocationType.CITY, 39.95, -75.17)
    assert conn.direct(ny, nearby, frozenset()) is not None


def test_no_walk_only_transit_candidate():
    # Regression: a mode-restricted CSA query could return a journey reached purely
    # by footpath (no vehicle), surfacing a long pure-walk "transit" itinerary.
    tt = Timetable()
    tt.add_stop(_stop("NX", 47.0, 7.0))
    tt.add_stop(_stop("NY", 45.0, 9.0))            # ~280 km away
    tt.add_footpath("NX", "NY", timedelta(hours=3))
    tt.add_footpath("NY", "NX", timedelta(hours=3))
    conn = GeometricConnector(tt.stops)
    origin = place("o", LocationType.HOTEL, 47.0, 7.0)
    dest = place("d", LocationType.HOTEL, 45.0, 9.0)
    results = plan(origin, dest, DEP, tt, conn, top_n=5)
    for it in results:                              # no long pure-walk itinerary
        assert any(leg.mode is not Mode.WALK for leg in it.legs)


def test_short_footpath_only_transit_is_kept():
    # A reasonable footpath-only route (within the walk cap) is a legitimate no-car
    # option and must be offered, even though it has no vehicle leg; only an
    # unreasonably long walk-only journey is dropped (see the 280 km case above).
    tt = Timetable()
    tt.add_stop(_stop("P", 47.0, 7.0))
    tt.add_stop(_stop("Q", 47.072, 7.0))           # ~8 km apart (within the cap)
    tt.add_footpath("P", "Q", timedelta(hours=2))
    tt.add_footpath("Q", "P", timedelta(hours=2))
    # small access radius so the origin reaches only P and the dest only Q,
    # forcing the P->Q leg to be the footpath route (not a direct drive to Q).
    conn = GeometricConnector(tt.stops, max_access_km=5.0)
    origin = place("o", LocationType.HOTEL, 47.0, 7.0)
    dest = place("d", LocationType.HOTEL, 47.072, 7.0)
    results = plan(origin, dest, DEP, tt, conn, top_n=5)
    assert any(all(leg.mode is Mode.WALK for leg in it.legs)
               and it.total_distance_km > 1.5 for it in results)


def test_implausible_detour_transit_is_dropped():
    """When the only transit route is a gross backtrack (the real through-service is
    missing from the feed), it is dropped rather than offered as a 'train'. Here the
    train detours via a hub far off the direct line; the direct ground stays."""
    tt = Timetable()
    tt.add_stop(_stop("A", 47.0, 7.0))     # at the origin
    tt.add_stop(_stop("H", 50.0, 7.0))     # a hub ~330 km off the direct line
    tt.add_stop(_stop("B", 47.0, 9.0))     # at the destination (~150 km from origin)
    tt.add_trip(make_trip("DETOUR", Mode.TRAIN, [
        ("A", "09:00", "09:00"), ("H", "12:00", "12:00"), ("B", "16:00", "16:00")]))
    conn = GeometricConnector(tt.stops)
    origin = place("o", LocationType.HOTEL, 47.0, 7.0)
    dest = place("d", LocationType.HOTEL, 47.0, 9.0)
    results = plan(origin, dest, DEP, tt, conn, top_n=5)
    assert results                                    # the direct ground option remains
    assert not any(leg.mode is Mode.TRAIN
                   for it in results for leg in it.legs)   # the detour train is dropped


def test_short_trip_is_pure_ground():
    tt = Timetable()
    tt.add_stop(_stop("Far1", 47.0, 7.0))
    tt.add_stop(_stop("Far2", 46.0, 8.0))
    tt.add_trip(make_trip("T", Mode.TRAIN, [
        ("Far1", "09:00", "09:00"), ("Far2", "10:00", "10:00")]))
    conn = GeometricConnector(tt.stops)
    # Origin/dest far from any station and close together.
    origin = place("A", LocationType.HOTEL, 40.0, -74.0)
    dest = place("B", LocationType.HOTEL, 40.01, -74.0)
    results = plan(origin, dest, DEP, tt, conn)
    assert len(results) == 1                     # only the pure-ground candidate
    assert len(results[0].legs) == 1
    assert results[0].primary_mode in (Mode.CAR, Mode.WALK)  # 1.1 km -> walked


def test_seasonal_ferry_present_in_summer_absent_in_winter():
    tt = Timetable()
    tt.add_stop(_stop("Xp", 46.0, 6.0, NodeType.FERRY_TERMINAL))
    tt.add_stop(_stop("Yp", 46.0, 7.0, NodeType.FERRY_TERMINAL))
    summer = Validity(open_months=frozenset({6, 7, 8, 9}))
    tt.add_trip(make_trip("ferry", Mode.FERRY, [
        ("Xp", "11:00", "11:00"), ("Yp", "12:00", "12:00")], validity=summer,
        cost_level=CostLevel.LOW))
    conn = GeometricConnector(tt.stops)
    origin = place("nearX", LocationType.HOTEL, 46.005, 6.005)
    dest = place("nearY", LocationType.HOTEL, 46.005, 6.995)

    # Price the crossing as a genuine ferry shortcut -- much cheaper than driving --
    # so the slower ferry stays non-dominated on the (now fare-based) cost axis.
    # (The default model prices this short straight-line hop about like the drive.)
    set_fare_model(heuristic_fare_model(rates={
        Mode.FERRY: (0.0, 0.02), Mode.CAR: (0.0, 0.60), Mode.WALK: (0.0, 0.0)}))
    try:
        summer_res = plan(origin, dest, datetime(2026, 7, 1, 8, 0), tt, conn)
        winter_res = plan(origin, dest, datetime(2026, 1, 15, 8, 0), tt, conn)
    finally:
        reset_fare_model()
    assert _has_mode(summer_res, Mode.FERRY)
    assert not _has_mode(winter_res, Mode.FERRY)


def test_flight_door_to_door():
    tt = Timetable()
    tt.add_stop(_stop("AxAir", 47.0, 7.0, NodeType.AIRPORT))
    tt.add_stop(_stop("AyAir", 46.0, 9.0, NodeType.AIRPORT))
    tt.add_trip(make_trip("F", Mode.FLIGHT, [
        ("AxAir", "09:00", "09:00"), ("AyAir", "10:00", "10:00")],
        cost_level=CostLevel.HIGH))
    conn = GeometricConnector(tt.stops)
    origin = place("nearAx", LocationType.HOTEL, 47.005, 7.005)
    dest = place("nearAy", LocationType.HOTEL, 46.005, 9.005)

    results = plan(origin, dest, DEP, tt, conn)
    assert results[0].primary_mode is Mode.FLIGHT
    assert results[0].cost_level is CostLevel.HIGH


def test_cch_connector_door_to_door():
    b = RoadGraphBuilder()
    b.add_node("bern", 46.95, 7.44)
    b.add_node("interlaken", 46.69, 7.86)
    b.add_node("brig", 46.32, 7.99)
    b.add_road("bern", "interlaken", 1500)        # 25 min drive
    b.add_road("interlaken", "brig", 100000)      # absurd road; train is better
    router = CCHRoadRouter(b.build())

    tt = Timetable()
    tt.add_stop(_stop("INT", 46.69, 7.86))
    tt.add_stop(_stop("BRG", 46.32, 7.99))
    tt.add_trip(make_trip("IC", Mode.TRAIN, [
        ("INT", "09:00", "09:00"), ("BRG", "11:00", "11:00")]))

    conn = CCHConnector(router, tt.stops,
                        stop_to_node={"INT": "interlaken", "BRG": "brig"})
    origin = place("nearBern", LocationType.HOTEL, 46.95, 7.45)
    dest = place("nearBrig", LocationType.HOTEL, 46.32, 8.00)

    results = plan(origin, dest, DEP, tt, conn)
    assert results
    top = results[0]
    # the absurd interlaken->brig road is avoided: the long haul is by train,
    # reached by a road access leg from Bern. (With accurate routed distances the
    # ~43 km access drive edges out the ~42 km train leg, so primary_mode is not
    # asserted -- the point is the train is used, not the 28 h road.)
    assert top.legs[0].mode is Mode.CAR          # road access from Bern
    assert any(leg.mode is Mode.TRAIN for leg in top.legs)
    assert len(top.legs) > 1                      # not the pure-drive candidate
    # the train arrives Brig 11:00; the final ~0.8 km hop to the door is walked
    assert top.legs[-1].mode is Mode.WALK
    assert datetime(2026, 7, 1, 11, 0) <= top.arrive_at <= datetime(2026, 7, 1, 11, 15)


def test_cch_connector_access_with_default_day():
    """day=None is valid per the RoadConnector protocol (current conditions);
    CCHConnector must not crash on a seasonally-validated graph."""
    b = RoadGraphBuilder()
    b.add_node("a", 47.0, 7.0)
    b.add_node("b", 47.0, 7.05)
    b.add_road("a", "b", 300)
    router = CCHRoadRouter(b.build())

    tt = Timetable()
    tt.add_stop(_stop("B", 47.0, 7.05))
    conn = CCHConnector(router, tt.stops, stop_to_node={"B": "b"})
    origin = place("nearA", LocationType.HOTEL, 47.0, 7.0)

    legs = conn.access(origin)               # day omitted -> defaults to today
    assert "B" in legs and legs["B"].mode is Mode.CAR


def test_cch_connector_leg_distance_is_routed_length():
    # Regression: leg distance was back-computed as time x 60 km/h. It must be the
    # real routed path length (haversine-sum of the path nodes).
    from travelplanner.geo import haversine
    b = RoadGraphBuilder()
    b.add_node("a", 47.0, 7.0)
    b.add_node("b", 47.0, 7.50)               # ~38 km east
    b.add_road("a", "b", 600)                 # fast 10-min road (not 60 km/h)
    router = CCHRoadRouter(b.build())
    tt = Timetable()
    tt.add_stop(_stop("B", 47.0, 7.50))
    conn = CCHConnector(router, tt.stops, stop_to_node={"B": "b"})
    origin = place("o", LocationType.HOTEL, 47.0, 7.0)
    leg = conn.access(origin, day=DEP.date())["B"]
    assert leg.mode is Mode.CAR
    expected = haversine(47.0, 7.0, 47.0, 7.50)
    assert abs(leg.distance_km - expected) < 1.0     # routed ~38 km
    assert leg.distance_km > 30                       # not the old 600s/3600*60 = 10 km


def test_cch_connector_drive_time_responds_to_departure_time():
    # Change A: the trip's departure time reaches the road speed model, so a
    # classified road's drive time rises at rush hour and eases at night. (Synthetic
    # arcs with no highway class stay at multiplier 1.0; this one is "primary".)
    b = RoadGraphBuilder()
    b.add_node("a", 47.0, 7.0)
    b.add_node("b", 47.0, 7.50)                       # ~38 km east
    b.add_road("a", "b", 600, highway="primary")      # classified: time-of-day applies
    router = CCHRoadRouter(b.build())
    tt = Timetable()
    tt.add_stop(_stop("B", 47.0, 7.50))
    conn = CCHConnector(router, tt.stops, stop_to_node={"B": "b"})
    origin = place("o", LocationType.HOTEL, 47.0, 7.0)
    dest = place("d", LocationType.HOTEL, 47.0, 7.50)

    peak = conn.direct(origin, dest, depart_at=datetime(2026, 7, 1, 8, 0))   # Wed rush
    night = conn.direct(origin, dest, depart_at=datetime(2026, 7, 1, 3, 0))  # Wed night
    avg = conn.direct(origin, dest, day=date(2026, 7, 1))                    # no time
    assert peak.seconds > avg.seconds > night.seconds   # rush > average > night
    # The routed distance is geometric and must not move with the clock.
    assert peak.distance_km == night.distance_km == avg.distance_km


def test_plan_surfaces_routed_polyline_on_car_leg():
    # Change B: a road-routed CAR leg carries the routed polyline end to end and
    # to_dict emits it. The path runs a -> c -> b (no direct a-b edge), so the
    # geometry has the intermediate node, not just the two endpoints.
    b = RoadGraphBuilder()
    b.add_node("a", 47.0, 7.0)
    b.add_node("c", 47.0, 7.25)
    b.add_node("b", 47.0, 7.50)
    b.add_road("a", "c", 300)
    b.add_road("c", "b", 300)
    router = CCHRoadRouter(b.build())
    tt = Timetable()                              # no stops -> the ground leg wins
    conn = CCHConnector(router, tt.stops)
    origin = place("o", LocationType.HOTEL, 47.0, 7.0)
    dest = place("d", LocationType.HOTEL, 47.0, 7.50)

    results = plan(origin, dest, DEP, tt, conn)
    car_legs = [leg for it in results for leg in it.legs if leg.mode is Mode.CAR]
    assert car_legs
    geom = car_legs[0].geometry
    assert geom is not None and len(geom) == 3            # a -> c -> b
    assert abs(geom[0][1] - 7.0) < 1e-4                   # starts at origin node
    assert abs(geom[1][1] - 7.25) < 1e-4                  # through the interior node
    assert abs(geom[-1][1] - 7.50) < 1e-4                 # ends at the dest node
    assert car_legs[0].to_dict()["geometry"] == [[p[0], p[1]] for p in geom]


def test_plan_prices_legs_with_active_fare_model():
    # The always-on heuristic fare model stamps every planned leg; the itinerary
    # total is the sum, in the model's currency. CAR is priced at 0.20 EUR/km.
    b = RoadGraphBuilder()
    b.add_node("a", 47.0, 7.0)
    b.add_node("b", 47.0, 7.50)
    b.add_road("a", "b", 600)
    router = CCHRoadRouter(b.build())
    tt = Timetable()
    conn = CCHConnector(router, tt.stops)
    origin = place("o", LocationType.HOTEL, 47.0, 7.0)
    dest = place("d", LocationType.HOTEL, 47.0, 7.50)

    it = plan(origin, dest, DEP, tt, conn)[0]
    assert it.fare_currency == "EUR"
    assert it.fare_estimate is not None and it.fare_estimate > 0
    car = next(leg for leg in it.legs if leg.mode is Mode.CAR)
    base, per_km = FARE_RATES[Mode.CAR]
    assert car.fare_estimate == round(base + per_km * car.distance_km, 2)
    assert abs(it.fare_estimate - car.fare_estimate) < 1e-9


def test_cch_connector_same_node_uses_geometric_estimate():
    # Regression: two distinct points that snap to the same road node yielded a
    # 0 km / 0 s drive. Fall back to a geometric estimate instead.
    from travelplanner.geo import haversine
    b = RoadGraphBuilder()
    b.add_node("a", 47.0, 7.0)
    b.add_node("far", 48.0, 8.0)
    b.add_road("a", "far", 600)
    router = CCHRoadRouter(b.build())
    tt = Timetable()
    tt.add_stop(_stop("NEARA", 47.05, 7.0))       # ~5.5 km from origin, snaps to 'a'
    conn = CCHConnector(router, tt.stops)
    origin = place("o", LocationType.HOTEL, 47.0, 7.0)
    leg = conn.access(origin, day=DEP.date())["NEARA"]
    assert leg.mode is Mode.CAR
    expected = haversine(47.0, 7.0, 47.05, 7.0)
    assert abs(leg.distance_km - expected) < 0.6   # ~5.5 km, not the old 0
    assert leg.seconds > 0


def test_cch_connector_caches_metric_per_day():
    # Regression: the road metric was cached on conditions only, so a connector
    # reused across dates served the first day's (seasonal) metric for every later
    # day. The cache key must include the day.
    b = RoadGraphBuilder()
    b.add_node("a", 47.0, 7.0)
    b.add_node("b", 47.0, 7.05)
    b.add_road("a", "b", 300)
    router = CCHRoadRouter(b.build())
    tt = Timetable()
    tt.add_stop(_stop("B", 47.0, 7.05))
    conn = CCHConnector(router, tt.stops, stop_to_node={"B": "b"})
    d1, d2 = date(2026, 1, 15), date(2026, 7, 15)
    assert conn._road(frozenset(), d1) is conn._road(frozenset(), d1)      # cached
    assert conn._road(frozenset(), d1) is not conn._road(frozenset(), d2)  # per-day


def test_cch_connector_rejects_out_of_coverage_point():
    # Regression: a point beyond the road coverage was snapped to an arbitrary far
    # node, fabricating a drive. Beyond max_snap_km it must yield no road leg.
    b = RoadGraphBuilder()
    b.add_node("a", 47.0, 7.0)
    b.add_node("b", 47.0, 7.02)
    b.add_road("a", "b", 120)
    router = CCHRoadRouter(b.build())
    tt = Timetable()
    tt.add_stop(_stop("FAR", 47.0, 7.40))      # ~29 km from the graph -> no road node
    conn = CCHConnector(router, tt.stops, max_access_km=60.0)
    origin = place("near", LocationType.HOTEL, 47.0, 7.0)
    assert "FAR" not in conn.access(origin, day=DEP.date())     # within range, off-grid
    far_dest = place("fardest", LocationType.HOTEL, 47.0, 9.0)  # ~150 km off-grid
    assert conn.direct(origin, far_dest, day=DEP.date()) is None


def test_cch_connector_walks_short_hops():
    """A sub-threshold hop is WALK, not CAR (matching GeometricConnector); a
    longer hop still drives the road network."""
    b = RoadGraphBuilder()
    b.add_node("near", 47.0, 7.000)
    b.add_node("far", 47.0, 7.060)
    b.add_road("near", "far", 400)
    router = CCHRoadRouter(b.build())

    tt = Timetable()
    tt.add_stop(_stop("NEAR", 47.0, 7.001))   # ~75 m from origin -> walk
    tt.add_stop(_stop("FAR", 47.0, 7.060))    # ~4.5 km -> drive
    conn = CCHConnector(router, tt.stops,
                        stop_to_node={"NEAR": "near", "FAR": "far"})
    origin = place("origin", LocationType.HOTEL, 47.0, 7.0)

    legs = conn.access(origin, day=DEP.date())
    assert legs["NEAR"].mode is Mode.WALK
    assert legs["FAR"].mode is Mode.CAR
    # a sub-threshold direct trip walks too
    nearby = place("nearby", LocationType.HOTEL, 47.0, 7.002)
    assert conn.direct(origin, nearby, day=DEP.date()).mode is Mode.WALK
