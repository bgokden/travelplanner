"""Stage 5: persist signals + restrictions + expanded order; load turn-aware offline."""

from datetime import date

from travelplanner.graph.road.model import RoadGraphBuilder
from travelplanner.graph.road import CCHRoadRouter
from travelplanner.graph.road.expanded import ExpandedCCHRoadRouter
from travelplanner.graph.road.store import (
    load_expanded_order,
    load_road_artifact,
    save_expanded_order,
    save_road_artifact,
)
from travelplanner.graph.road.turns import TurnCosts, build_expanded_graph
from travelplanner.speed import free_flow_model

DAY = date(2026, 6, 15)
FF = free_flow_model


def _junction(restrict_straight=False):
    b = RoadGraphBuilder(store_names=False)
    idx = {}
    for k, (lat, lon) in {"a": (47.0, 8.98), "C": (47.0, 9.0), "e": (47.0, 9.02),
                          "n": (47.02, 9.0), "s": (46.98, 9.0)}.items():
        idx[k] = b.add_node(k, lat, lon)
    arcs = {}
    arcs["aC"] = b.add_arc("a", "C", 100)
    for spoke in ("e", "n", "s"):
        arcs[f"C{spoke}"] = b.add_arc("C", spoke, 100)
    b.mark_signal(idx["C"])
    if restrict_straight:
        b.set_restricted_turns({(arcs["aC"], arcs["Ce"])})   # ban a->C->e
    return b.build()


def test_store_v3_signals_restrictions(tmp_path):
    g = _junction(restrict_straight=True)
    assert g.signal_nodes and g.restricted_turns
    save_road_artifact(g, CCHRoadRouter(g).order, str(tmp_path))
    h, _ = load_road_artifact(str(tmp_path))
    assert h.signal_nodes == g.signal_nodes
    assert h.restricted_turns == g.restricted_turns


def test_expanded_order_roundtrip(tmp_path):
    g = _junction()
    r = ExpandedCCHRoadRouter(build_expanded_graph(g, turn_costs=TurnCosts()))
    save_expanded_order(r.order, str(tmp_path))
    assert load_expanded_order(str(tmp_path)) == r.order
    assert load_expanded_order(str(tmp_path / "empty")) is None


def test_turn_aware_offline_matches_inmemory(tmp_path):
    g = _junction(restrict_straight=True)
    mem = ExpandedCCHRoadRouter(build_expanded_graph(g, turn_costs=TurnCosts()))
    a, n = g.index("a"), g.index("n")
    mem_path = mem.customize(DAY, speed_model=FF).route_index(a, n)

    # persist (base v3 + expanded order), then load with no rebuild of the order
    save_road_artifact(g, CCHRoadRouter(g).order, str(tmp_path))
    save_expanded_order(mem.order, str(tmp_path))
    h, _ = load_road_artifact(str(tmp_path))
    off = ExpandedCCHRoadRouter(build_expanded_graph(h, turn_costs=TurnCosts()),
                                order=load_expanded_order(str(tmp_path)))
    off_path = off.customize(DAY, speed_model=FF).route_index(a, n)

    assert off_path is not None
    assert off_path.seconds == mem_path.seconds
    assert off_path.node_indices == mem_path.node_indices
    # the restriction survived: a->C->e is banned, so straight to 'e' is blocked
    blocked = off.customize(DAY, speed_model=FF).route_index(a, g.index("e"))
    assert blocked is None
