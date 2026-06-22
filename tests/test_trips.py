"""Tests for the door-to-door plan_trip glue (geocode + connector choice + plan)."""

from datetime import datetime

import pytest

from travelplanner.models import Itinerary, LocationType, Mode
from travelplanner.graph.coupling import GeometricConnector, SplitConnector
from travelplanner.graph.query import Objective
from travelplanner.graph.scheduled.model import Stop, Timetable, make_trip
from travelplanner.samples import sample_timetable, sample_trip
from travelplanner.trips import plan_trip
from travelplanner import place


def test_plan_trip_geometric_default():
    origin, dest, depart = sample_trip()
    tt = sample_timetable()
    result = plan_trip(origin, dest, depart, tt)
    assert isinstance(result, list)
    assert result and len(result) <= 3
    assert all(isinstance(it, Itinerary) for it in result)


def test_plan_trip_accepts_coord_forms():
    """Origin as a "lat,lon" string and dest as a (lat, lon) tuple both coerce."""
    _, _, depart = sample_trip()
    tt = sample_timetable()
    result = plan_trip("47.0,7.005", (45.0, 9.01), depart, tt)
    assert result


def test_plan_trip_objective_passthrough():
    """The objective reaches plan(): air-priority leads with the flight,
    cheapest does not."""
    origin, dest, depart = sample_trip()
    tt = sample_timetable()
    air = plan_trip(origin, dest, depart, tt, objective=Objective.AIR_PRIORITY)
    cheap = plan_trip(origin, dest, depart, tt, objective=Objective.CHEAPEST)
    assert air[0].primary_mode is Mode.FLIGHT
    assert cheap[0].primary_mode is not Mode.FLIGHT


def test_plan_trip_greenest_objective():
    """GREENEST flows through plan_trip and prefers the lower-driving option."""
    tt = _airport_train_timetable()
    origin = place("Amsterdam centre", LocationType.HOTEL, 52.3702, 4.8952)
    dest = place("Vaduz", LocationType.HOTEL, 47.1410, 9.5215)
    depart = datetime(2026, 6, 17, 7, 30)
    # transit access generates the no-car walk->train->flight option; GREENEST keeps it first
    best = plan_trip(origin, dest, depart, tt, access="transit",
                     objective=Objective.GREENEST)[0]
    assert not any(leg.mode is Mode.CAR for leg in best.legs)


def test_plan_trip_top_n():
    origin, dest, depart = sample_trip()
    tt = sample_timetable()
    result = plan_trip(origin, dest, depart, tt, top_n=1)
    assert len(result) == 1


def test_plan_trip_explicit_connector_wins():
    """An explicit connector overrides road/region selection. With access/egress
    disabled, only the pure-ground candidate survives (no transit legs)."""
    origin, dest, depart = sample_trip()
    tt = sample_timetable()
    ground_only = GeometricConnector(tt.stops, max_access_km=0.0,
                                     max_ground_km=10_000.0)
    result = plan_trip(origin, dest, depart, tt, road=True, region="nonsense",
                       connector=ground_only)
    assert result
    modes = {leg.mode for it in result for leg in it.legs}
    assert modes <= {Mode.CAR, Mode.WALK}   # never reached a station


def test_plan_trip_validates_modes_before_geocoding():
    # An invalid mode flag must surface the mode error, not a geocoding error,
    # even when the origin is unresolvable (cheap validation runs first).
    _, _, depart = sample_trip()
    tt = sample_timetable()
    with pytest.raises(ValueError, match="access"):
        plan_trip("Nonexistentplace_xyz", "0,0", depart, tt, access="bike")


def test_plan_trip_connector_overrides_conflicting_flags():
    # An explicit connector fully defines access/egress, so flag combos that are
    # otherwise rejected (access='both' with a separate egress) must not raise.
    origin, dest, depart = sample_trip()
    tt = sample_timetable()
    conn = GeometricConnector(tt.stops)
    result = plan_trip(origin, dest, depart, tt, connector=conn,
                       access="both", egress="car")
    assert isinstance(result, list)


def test_plan_trip_no_route_returns_empty():
    """No access, no feasible direct ground -> empty list (not an error)."""
    origin, dest, depart = sample_trip()
    tt = sample_timetable()
    blocked = GeometricConnector(tt.stops, max_access_km=0.0, max_ground_km=0.0)
    assert plan_trip(origin, dest, depart, tt, connector=blocked) == []


def test_plan_trip_uncovered_endpoint_falls_back_to_geometric(monkeypatch):
    """road=True where no single region covers both AND an endpoint is not
    covered at all (across water, e.g. Amsterdam->London) cannot split either, so
    it degrades to the geometric connector instead of raising."""
    origin, dest, depart = sample_trip()
    tt = sample_timetable()

    def _no_single(region, data_dir, coords):
        raise ValueError("cross-border: no single region covers both points")

    def _uncovered(lat, lon):
        raise ValueError("no region covers this point")

    monkeypatch.setattr("travelplanner.trips._auto_region", _no_single)
    monkeypatch.setattr("travelplanner.geofabrik.region_for", _uncovered)
    result = plan_trip(origin, dest, depart, tt, road=True)
    assert result == plan_trip(origin, dest, depart, tt)   # same as geometric


def test_plan_trip_bad_coordinate_raises():
    _, _, depart = sample_trip()
    tt = sample_timetable()
    with pytest.raises(ValueError):
        plan_trip("95.0,0.0", (45.0, 9.01), depart, tt)


def test_split_connector_delegates_per_endpoint():
    """access uses the origin-side connector, egress the destination-side one;
    direct is the optional ground fallback (None without it)."""
    west = {"W": Stop("W", "West", 47.0, 7.0)}
    east = {"E": Stop("E", "East", 45.0, 9.0)}
    acc = GeometricConnector(west, max_access_km=50.0)
    egr = GeometricConnector(east, max_access_km=50.0)
    origin = place("o", LocationType.HOTEL, 47.0, 7.01)
    dest = place("d", LocationType.HOTEL, 45.0, 9.01)

    sc = SplitConnector(acc, egr)
    assert set(sc.access(origin)) == {"W"}      # origin region only
    assert set(sc.egress(dest)) == {"E"}        # destination region only
    assert sc.direct(origin, dest) is None      # no ground candidate without one

    sc2 = SplitConnector(acc, egr,
                         direct_connector=GeometricConnector({}, max_ground_km=1e5))
    assert sc2.direct(origin, dest) is not None


def test_plan_trip_inter_region_builds_split(monkeypatch):
    """road=True across two regions (single-region resolution raises) builds a
    SplitConnector online instead of falling back to geometric."""
    origin, dest, depart = sample_trip()        # ~(47,7) -> (45,9)
    tt = sample_timetable()
    built = []

    def _no_single(region, data_dir, coords):
        raise ValueError("cross-region")

    class _Region:
        def __init__(self, url):
            self.pbf_url = url

    def _region_for(lat, lon):
        return _Region("west" if lon < 8 else "east")

    def _region_connector(region, stops, **kwargs):
        built.append(region)
        return GeometricConnector(stops)

    monkeypatch.setattr("travelplanner.trips._auto_region", _no_single)
    monkeypatch.setattr("travelplanner.geofabrik.region_for", _region_for)
    monkeypatch.setattr("travelplanner.trips.region_connector", _region_connector)

    result = plan_trip(origin, dest, depart, tt, road=True)
    assert result
    assert set(built) == {"west", "east"}       # one connector per endpoint


def test_plan_trip_inter_region_offline_falls_back(monkeypatch):
    """A single data_dir cannot hold two regions, so an offline cross-region trip
    falls back to geometric rather than splitting."""
    origin, dest, depart = sample_trip()
    tt = sample_timetable()

    def _no_single(region, data_dir, coords):
        raise ValueError("cross-region")

    monkeypatch.setattr("travelplanner.trips._auto_region", _no_single)
    result = plan_trip(origin, dest, depart, tt, road=True, data_dir="/some/dir")
    assert result == plan_trip(origin, dest, depart, tt)     # geometric


def test_plan_trip_turn_aware_requires_road():
    origin, dest, depart = sample_trip()
    tt = sample_timetable()
    with pytest.raises(ValueError):
        plan_trip(origin, dest, depart, tt, turn_aware=True)   # road=False


def _airport_train_timetable():
    """Local train Amsterdam Centraal -> Schiphol, then a flight onward, so the
    access to the airport can itself be transit."""
    from travelplanner.graph.schema import NodeType
    tt = Timetable()
    tt.add_stop(Stop("ASD_CS", "Amsterdam Centraal", 52.3791, 4.9003,
                     NodeType.RAIL_STATION))
    tt.add_stop(Stop("SPL", "Schiphol", 52.3105, 4.7683, NodeType.AIRPORT))
    tt.add_stop(Stop("VAD", "Vaduz Airfield", 47.140, 9.510, NodeType.AIRPORT))
    tt.add_trip(make_trip("IC", Mode.TRAIN,
                          [("ASD_CS", "09:00", "09:00"), ("SPL", "09:16", "09:16")]))
    tt.add_trip(make_trip("FL", Mode.FLIGHT,
                          [("SPL", "10:00", "10:00"), ("VAD", "11:30", "11:30")]))
    return tt


def test_plan_trip_transit_access_takes_the_train():
    """access='transit' walks to the nearest station and takes the train to the
    airport (line-haul) instead of driving -- no car leg."""
    tt = _airport_train_timetable()
    origin = place("Amsterdam centre", LocationType.HOTEL, 52.3702, 4.8952)  # ~1km from Centraal
    dest = place("Vaduz", LocationType.HOTEL, 47.1410, 9.5215)               # ~1km from VAD
    depart = datetime(2026, 6, 17, 7, 30)

    res = plan_trip(origin, dest, depart, tt, access="transit")
    assert res
    best = res[0]
    modes = [leg.mode for leg in best.legs]
    assert Mode.CAR not in modes                 # no driving
    assert modes[0] is Mode.WALK                 # walk to the station
    assert Mode.TRAIN in modes and Mode.FLIGHT in modes


def test_plan_trip_car_access_default_drives_to_airport():
    """The default (access='car') drives straight to the airport, so the train
    option is dominated and absent -- the contrast that motivates 'transit'."""
    tt = _airport_train_timetable()
    origin = place("Amsterdam centre", LocationType.HOTEL, 52.3702, 4.8952)
    dest = place("Vaduz", LocationType.HOTEL, 47.1410, 9.5215)
    depart = datetime(2026, 6, 17, 7, 30)

    best = plan_trip(origin, dest, depart, tt)[0]
    assert best.legs[0].mode is Mode.CAR
    assert Mode.TRAIN not in [leg.mode for leg in best.legs]


def test_plan_trip_access_both_shows_car_and_transit():
    """access='both' pools car and transit candidates so BOTH the drive-to-airport
    flight and the walk-to-train itinerary appear on one frontier. The low-car
    option is a strictly slower trade-off, so it is surfaced under GREENEST (where
    it ranks), not padded into the time-ordered default result."""
    tt = _airport_train_timetable()
    origin = place("Amsterdam centre", LocationType.HOTEL, 52.3702, 4.8952)
    dest = place("Vaduz", LocationType.HOTEL, 47.1410, 9.5215)
    depart = datetime(2026, 6, 17, 7, 30)

    res = plan_trip(origin, dest, depart, tt, access="both",
                    objective=Objective.GREENEST, top_n=5)
    first_legs = {it.legs[0].mode for it in res}
    assert Mode.CAR in first_legs and Mode.WALK in first_legs   # both access modes present
    assert not any(leg.mode is Mode.CAR for leg in res[0].legs)  # GREENEST leads no-car


def test_plan_trip_asymmetric_transit_access_car_egress():
    """access='transit', egress='car': walk to the station for the line-haul,
    then a car from the arrival stop to a door that is too far to walk."""
    tt = _airport_train_timetable()
    origin = place("Amsterdam centre", LocationType.HOTEL, 52.3702, 4.8952)   # ~1 km from Centraal
    dest = place("Liechtenstein countryside", LocationType.HOTEL, 47.18, 9.55)  # ~5 km from VAD
    depart = datetime(2026, 6, 17, 7, 30)

    best = plan_trip(origin, dest, depart, tt, access="transit", egress="car")[0]
    assert best.legs[0].mode is Mode.WALK       # transit access (walk to the train)
    assert best.legs[-1].mode is Mode.CAR       # car egress to the far door
    assert Mode.TRAIN in [leg.mode for leg in best.legs]


def test_plan_trip_egress_matches_access_when_unset():
    """egress=None behaves exactly like the symmetric access mode."""
    tt = _airport_train_timetable()
    origin = place("Amsterdam centre", LocationType.HOTEL, 52.3702, 4.8952)
    dest = place("Vaduz", LocationType.HOTEL, 47.1410, 9.5215)
    depart = datetime(2026, 6, 17, 7, 30)
    a = plan_trip(origin, dest, depart, tt, access="transit")
    b = plan_trip(origin, dest, depart, tt, access="transit", egress="transit")
    assert [leg.mode for leg in a[0].legs] == [leg.mode for leg in b[0].legs]


def test_plan_trip_egress_car_equals_default_car():
    """access='car', egress='car' (equal, non-None) takes the plain car path, not
    the asymmetric SplitConnector branch -- identical to the default."""
    tt = _airport_train_timetable()
    origin = place("Amsterdam centre", LocationType.HOTEL, 52.3702, 4.8952)
    dest = place("Vaduz", LocationType.HOTEL, 47.1410, 9.5215)
    depart = datetime(2026, 6, 17, 7, 30)
    a = plan_trip(origin, dest, depart, tt)                          # default car
    b = plan_trip(origin, dest, depart, tt, access="car", egress="car")
    assert [leg.mode for leg in a[0].legs] == [leg.mode for leg in b[0].legs]


def test_plan_trip_asymmetric_car_access_transit_egress():
    """access='car', egress='transit': drive to the airport, walk the last mile."""
    tt = _airport_train_timetable()
    origin = place("North of Amsterdam", LocationType.HOTEL, 52.424, 4.900)  # ~5 km from Centraal
    dest = place("Near Vaduz", LocationType.HOTEL, 47.149, 9.510)            # ~1 km from VAD
    depart = datetime(2026, 6, 17, 7, 30)
    best = plan_trip(origin, dest, depart, tt, access="car", egress="transit")[0]
    assert best.legs[0].mode is Mode.CAR        # car access (drive to the airport)
    assert best.legs[-1].mode is Mode.WALK      # transit egress (walk from the airport)
    assert Mode.FLIGHT in [leg.mode for leg in best.legs]


def test_plan_trip_asymmetric_direct_matches_symmetric_short_hop():
    """The direct (no-transit) ground leg follows the first-mile mode, so a short
    transit-access trip walks whether or not egress differs (review fix: the
    asymmetric direct connector used to use the 1.5 km car threshold and drove)."""
    tt = Timetable()
    tt.add_stop(Stop("FAR", "Far station", 40.0, 0.0))          # out of access range
    origin = place("A", LocationType.HOTEL, 52.000, 0.000)
    dest = place("B", LocationType.HOTEL, 52.000, 0.026)        # ~1.8 km east (>1.5, <2.0)
    depart = datetime(2026, 6, 17, 8, 0)
    sym = plan_trip(origin, dest, depart, tt, access="transit")[0]
    asym = plan_trip(origin, dest, depart, tt, access="transit", egress="car")[0]
    assert sym.legs[0].mode is Mode.WALK
    assert asym.legs[0].mode is Mode.WALK


def test_plan_trip_access_invalid_raises():
    origin, dest, depart = sample_trip()
    with pytest.raises(ValueError):
        plan_trip(origin, dest, depart, sample_timetable(), access="bike")


def test_plan_trip_egress_invalid_raises():
    origin, dest, depart = sample_trip()
    with pytest.raises(ValueError):
        plan_trip(origin, dest, depart, sample_timetable(), egress="bike")


def test_plan_trip_egress_invalid_both_raises():
    origin, dest, depart = sample_trip()
    with pytest.raises(ValueError):
        plan_trip(origin, dest, depart, sample_timetable(), egress="both")


def test_plan_trip_both_with_egress_raises():
    origin, dest, depart = sample_trip()
    with pytest.raises(ValueError):
        plan_trip(origin, dest, depart, sample_timetable(),
                  access="both", egress="car")


def test_plan_trip_asymmetric_with_road_raises():
    origin, dest, depart = sample_trip()
    with pytest.raises(ValueError):
        plan_trip(origin, dest, depart, sample_timetable(),
                  access="transit", egress="car", road=True)


def test_plan_trip_access_both_with_road_raises():
    origin, dest, depart = sample_trip()
    with pytest.raises(ValueError):
        plan_trip(origin, dest, depart, sample_timetable(),
                  access="both", road=True)


def test_plan_trip_transit_access_with_road_raises():
    origin, dest, depart = sample_trip()
    with pytest.raises(ValueError):
        plan_trip(origin, dest, depart, sample_timetable(),
                  access="transit", road=True)


def _flight_only_timetable():
    """A one-flight network (Schiphol -> Zurich) standing in for the OpenFlights
    data the auto-source path fetches; returned by the load_openflights seam."""
    from travelplanner.graph.schema import NodeType
    tt = Timetable()
    tt.add_stop(Stop("AAA", "Schiphol", 52.31, 4.76, NodeType.AIRPORT,
                     tz="Europe/Amsterdam"))
    tt.add_stop(Stop("BBB", "Zurich Airport", 47.46, 8.55, NodeType.AIRPORT,
                     tz="Europe/Zurich"))
    tt.add_trip(make_trip("AAA-BBB", Mode.FLIGHT,
                          [("AAA", "10:00", "10:00"), ("BBB", "11:30", "11:30")]))
    return tt


def test_plan_trip_without_timetable_autocomposes_and_routes(monkeypatch):
    """The public auto-source path end to end: with NO timetable, plan_trip composes
    one (flights via the OpenFlights seam, no ground feed) and routes a door-to-door
    itinerary through it -- car access -> flight -> car egress -- while surfacing the
    ground-coverage gap as a warning."""
    from travelplanner import auto_timetable

    flights = _flight_only_timetable()
    monkeypatch.setattr(auto_timetable, "airports_near",
                        lambda pts, r, download: {"AAA", "BBB"})
    monkeypatch.setattr(auto_timetable, "load_openflights",
                        lambda keep, download: flights)
    monkeypatch.setattr(auto_timetable, "catalog", lambda: {})   # no ground feed

    origin = place("Amsterdam", LocationType.CITY, 52.37, 4.90)
    dest = place("Zurich", LocationType.CITY, 47.38, 8.54)
    depart = datetime(2026, 6, 23, 7, 0)

    with pytest.warns(UserWarning, match="no GTFS feed"):
        result = plan_trip(origin, dest, depart)                 # timetable omitted

    assert result                                                # composed AND routed
    assert result[0].primary_mode is Mode.FLIGHT                 # AIR_PRIORITY default
    assert any(leg.mode is Mode.FLIGHT for it in result for leg in it.legs)


def test_plan_trip_without_timetable_no_data_degrades(monkeypatch):
    """No air and no ground data: the public path does not crash -- it surfaces the
    coverage gaps as warnings and still returns the direct ground (drive) option."""
    from travelplanner import auto_timetable

    monkeypatch.setattr(auto_timetable, "airports_near",
                        lambda pts, r, download: set())          # no airports
    monkeypatch.setattr(auto_timetable, "catalog", lambda: {})   # no ground feed

    origin = place("Amsterdam", LocationType.CITY, 52.37, 4.90)
    dest = place("Utrecht", LocationType.CITY, 52.09, 5.12)      # ~37 km, drivable
    depart = datetime(2026, 6, 23, 7, 0)

    with pytest.warns(UserWarning):                              # gap notes surface
        result = plan_trip(origin, dest, depart)

    assert result                                                # direct ground option
    assert all(leg.mode in (Mode.CAR, Mode.WALK)
               for it in result for leg in it.legs)              # no transit data used


def _turn_graph():
    """A real junction at C (>=3 neighbours) with a signal, so turning movements
    carry a junction/signal cost the node-based engine cannot see."""
    from travelplanner.graph.road.model import RoadGraphBuilder

    b = RoadGraphBuilder()
    coords = {"a": (47.0, 8.98), "C": (47.0, 9.0), "e": (47.0, 9.02),
              "n": (47.02, 9.0), "s": (46.98, 9.0)}
    idx = {k: b.add_node(k, lat, lon) for k, (lat, lon) in coords.items()}
    b.add_arc("a", "C", 100)
    b.add_arc("C", "a", 100)
    for spoke in ("e", "n", "s"):
        b.add_arc("C", spoke, 100)
        b.add_arc(spoke, "C", 100)
    b.mark_signal(idx["C"])
    return b.build()


def test_cch_connector_turn_aware_changes_duration():
    """The path region_connector(turn_aware=True) builds: a CCHConnector backed
    by the edge-expanded router. The turning movement a->C->n costs more than the
    node-based estimate, and the new ExpandedCustomized.route(key,key) is what the
    connector calls."""
    from datetime import date

    from travelplanner.graph.road import CCHRoadRouter
    from travelplanner.graph.road.expanded import ExpandedCCHRoadRouter
    from travelplanner.graph.road.turns import TurnCosts, build_expanded_graph
    from travelplanner.speed import free_flow_model

    g = _turn_graph()
    node = CCHRoadRouter(g)
    turn = ExpandedCCHRoadRouter(build_expanded_graph(g, turn_costs=TurnCosts()))
    day, ff = date(2026, 6, 15), free_flow_model

    base = node.customize(day).route_index(g.index("a"), g.index("n"))
    # the new key-based mirror returns the same path as route_index
    by_key = turn.customize(day, speed_model=ff).route("a", "n")
    by_idx = turn.customize(day, speed_model=ff).route_index(g.index("a"), g.index("n"))
    assert by_key.seconds == by_idx.seconds
    # turning at a signalled junction costs strictly more than node-based
    assert by_key.seconds > base.seconds
