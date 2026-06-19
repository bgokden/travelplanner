"""Tests for the door-to-door plan_trip glue (geocode + connector choice + plan)."""

from datetime import datetime

import pytest

from travelplanner.models import Itinerary, Mode
from travelplanner.graph.coupling import GeometricConnector
from travelplanner.graph.query import Objective
from travelplanner.samples import sample_timetable, sample_trip
from travelplanner.trips import plan_trip


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


def test_plan_trip_no_route_returns_empty():
    """No access, no feasible direct ground -> empty list (not an error)."""
    origin, dest, depart = sample_trip()
    tt = sample_timetable()
    blocked = GeometricConnector(tt.stops, max_access_km=0.0, max_ground_km=0.0)
    assert plan_trip(origin, dest, depart, tt, connector=blocked) == []


def test_plan_trip_cross_border_falls_back_to_geometric(monkeypatch):
    """road=True where no single region covers both endpoints degrades to the
    geometric connector instead of raising."""
    origin, dest, depart = sample_trip()
    tt = sample_timetable()

    def _raise(region, data_dir, coords):
        raise ValueError("cross-border: no single region covers both points")

    monkeypatch.setattr("travelplanner.trips._auto_region", _raise)
    result = plan_trip(origin, dest, depart, tt, road=True)
    assert result == plan_trip(origin, dest, depart, tt)   # same as geometric


def test_plan_trip_bad_coordinate_raises():
    _, _, depart = sample_trip()
    tt = sample_timetable()
    with pytest.raises(ValueError):
        plan_trip("95.0,0.0", (45.0, 9.01), depart, tt)


def test_plan_trip_turn_aware_requires_road():
    origin, dest, depart = sample_trip()
    tt = sample_timetable()
    with pytest.raises(ValueError):
        plan_trip(origin, dest, depart, tt, turn_aware=True)   # road=False


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
    pytest.importorskip("routingkit_cch")
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
