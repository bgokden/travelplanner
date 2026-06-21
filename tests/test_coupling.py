"""Door-to-door coupling tests (Phase 3)."""

from datetime import date, datetime, timedelta

from travelplanner import place
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


def test_plan_rejects_phantom_stop():
    # Regression: a dangling stop reference (a footpath/trip naming a stop with no
    # Stop entry) crashed journey reconstruction with an opaque KeyError; the
    # timetable is now validated up front with a clear ValueError.
    import pytest
    tt = Timetable()
    tt.add_stop(_stop("S", 47.0, 7.0))
    tt.add_stop(_stop("D", 46.0, 8.0))
    tt.add_footpath("S", "GHOST", timedelta(minutes=5))
    tt.add_trip(make_trip("TR", Mode.TRAIN, [
        ("GHOST", "09:00", "09:00"), ("D", "10:00", "10:00")]))
    conn = GeometricConnector(tt.stops)
    origin = place("o", LocationType.HOTEL, 47.0, 7.0)
    dest = place("d", LocationType.HOTEL, 46.0, 8.0)
    with pytest.raises(ValueError, match="GHOST"):
        plan(origin, dest, DEP, tt, conn)


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
    # Cheaper than driving, so the ferry stays on the Pareto frontier even
    # though driving the 77 km straight line is faster.
    tt.add_trip(make_trip("ferry", Mode.FERRY, [
        ("Xp", "11:00", "11:00"), ("Yp", "12:00", "12:00")], validity=summer,
        cost_level=CostLevel.LOW))
    conn = GeometricConnector(tt.stops)
    origin = place("nearX", LocationType.HOTEL, 46.005, 6.005)
    dest = place("nearY", LocationType.HOTEL, 46.005, 6.995)

    summer_res = plan(origin, dest, datetime(2026, 7, 1, 8, 0), tt, conn)
    winter_res = plan(origin, dest, datetime(2026, 1, 15, 8, 0), tt, conn)
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
