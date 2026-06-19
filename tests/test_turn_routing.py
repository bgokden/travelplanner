"""Stage 2: turn-aware routing over the edge-expanded graph (needs routingkit)."""

from datetime import date

import pytest

from travelplanner.graph.road.model import RoadGraphBuilder
from travelplanner.graph.road.turns import build_expanded_graph

routingkit = pytest.importorskip("routingkit_cch")

from travelplanner.graph.road import CCHRoadRouter  # noqa: E402
from travelplanner.graph.road.expanded import ExpandedCCHRoadRouter  # noqa: E402
from travelplanner.speed import free_flow_model  # noqa: E402

DAY = date(2026, 6, 15)
FF = free_flow_model  # isolate turn logic from the speed multiplier


def _grid():
    # 3 collinear nodes a-b-c plus a parallel detour a-d-c. Straightforward route
    # a->c is a-b-c. No U-turns needed.
    b = RoadGraphBuilder(store_names=False)
    for k, (lat, lon) in {"a": (47.10, 9.50), "b": (47.10, 9.52),
                          "c": (47.10, 9.54), "d": (47.11, 9.52)}.items():
        b.add_node(k, lat, lon)
    b.add_road("a", "b", 100, highway="residential")
    b.add_road("b", "c", 100, highway="residential")
    b.add_road("a", "d", 200, highway="residential")
    b.add_road("d", "c", 200, highway="residential")
    return b.build()


def _idx(g, name):
    return g.index(name)


def test_turn_aware_matches_node_based_when_no_turn_cost():
    g = _grid()
    base = CCHRoadRouter(g)
    exp = ExpandedCCHRoadRouter(build_expanded_graph(g, turn_seconds=0,
                                                     uturn_seconds=120))
    a, c = _idx(g, "a"), _idx(g, "c")
    node_path = base.customize(DAY, speed_model=FF).route("a", "c")
    exp_path = exp.customize(DAY, speed_model=FF).route_index(a, c)
    # same total time (no turns penalized on the straight a-b-c route)
    assert exp_path is not None
    assert exp_path.seconds == node_path.seconds == 200
    assert g.key(exp_path.node_indices[0]) == "a"
    assert g.key(exp_path.node_indices[-1]) == "c"


def test_turn_cost_increases_time():
    g = _grid()
    exp = ExpandedCCHRoadRouter(build_expanded_graph(g, turn_seconds=30,
                                                     uturn_seconds=120))
    a, c = _idx(g, "a"), _idx(g, "c")
    path = exp.customize(DAY, speed_model=FF).route_index(a, c)
    # a-b-c has one turn at b -> +30s over the 200s travel
    assert path.seconds == 230


def test_forbidden_turn_forces_detour():
    g = _grid()
    # forbid the a->b ... b->c turn at b, forcing the longer a-d-c detour
    ab = next(i for i in range(g.arc_count)
              if g.tail[i] == g.index("a") and g.head[i] == g.index("b"))
    bc = next(i for i in range(g.arc_count)
              if g.tail[i] == g.index("b") and g.head[i] == g.index("c"))
    exp = ExpandedCCHRoadRouter(build_expanded_graph(g, turn_seconds=0,
                                                     forbidden={(ab, bc)}))
    path = exp.customize(DAY, speed_model=FF).route_index(g.index("a"), g.index("c"))
    assert path is not None
    assert [g.key(i) for i in path.node_indices] == ["a", "d", "c"]
    assert path.seconds == 400          # the detour, not the blocked 200 route


def test_single_arc_route():
    g = _grid()
    exp = ExpandedCCHRoadRouter(build_expanded_graph(g))
    path = exp.customize(DAY, speed_model=FF).route_index(g.index("a"), g.index("b"))
    assert path is not None
    assert path.seconds == 100
    assert [g.key(i) for i in path.node_indices] == ["a", "b"]
