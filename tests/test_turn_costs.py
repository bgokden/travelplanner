"""Stage 4: geometric turn-cost classification + traffic-signal surcharge."""

from datetime import date

from travelplanner.geo import turn_angle
from travelplanner.graph.road.model import RoadGraphBuilder
from travelplanner.graph.road.turns import TurnCosts, build_expanded_graph
from travelplanner.graph.road.expanded import ExpandedCCHRoadRouter
from travelplanner.speed import free_flow_model


def test_turn_angle_sign():
    # heading east (90) then north (0) is a LEFT turn (negative)
    assert turn_angle(90, 0) == -90
    # heading east then south (180) is a RIGHT turn (positive)
    assert turn_angle(90, 180) == 90
    assert abs(turn_angle(0, 5)) == 5            # nearly straight


def test_turn_costs_classification():
    tc = TurnCosts()
    assert tc.cost(False, 0, False) == tc.straight
    assert tc.cost(False, 90, False) == tc.favorable     # right (drive-on-right)
    assert tc.cost(False, -90, False) == tc.unfavorable  # left
    assert tc.cost(True, 180, False) == tc.uturn
    assert tc.cost(False, 90, True) == tc.favorable + tc.signal   # + signal


def test_left_hand_traffic_mirrors():
    tc = TurnCosts(drive_on_right=False)
    assert tc.cost(False, 90, False) == tc.unfavorable   # right is now unfavorable
    assert tc.cost(False, -90, False) == tc.favorable


def test_hairpin_onto_other_road_is_sharp_not_uturn():
    # A near-180 deg angle that is NOT a topological U-turn (is_uturn=False) is a
    # hairpin onto a different road -> sharp cost, not the heavier U-turn cost.
    tc = TurnCosts()
    assert tc.cost(False, 170, False) == tc.sharp
    assert tc.cost(False, 180, False) == tc.sharp
    assert tc.sharp < tc.uturn
    # a real U-turn (is_uturn=True) still gets the full U-turn penalty
    assert tc.cost(True, 180, False) == tc.uturn


# --- routing effect -------------------------------------------------------

DAY = date(2026, 6, 15)


def _T(graph):
    exp = ExpandedCCHRoadRouter(build_expanded_graph(graph, turn_costs=TurnCosts()))
    return exp.customize(DAY, speed_model=free_flow_model)


def _junction(signal=False):
    # A 4-way junction at b: a(west)->b->{n,e,s}. b has 4 neighbours -> a real
    # junction, so turns there incur a cost; heading east at b, n=left, s=right.
    b = RoadGraphBuilder(store_names=False)
    idx = {}
    for k, (lat, lon) in {"a": (47.00, 8.98), "b": (47.00, 9.00),
                          "n": (47.02, 9.00), "e": (47.00, 9.02),
                          "s": (46.98, 9.00)}.items():
        idx[k] = b.add_node(k, lat, lon)
    for spoke in ("n", "e", "s"):
        b.add_road("b", spoke, 100, highway="primary")
    b.add_road("a", "b", 100, highway="primary")
    if signal:
        b.mark_signal(idx["b"])
    return b.build()


def test_no_cost_at_non_junction():
    # a-b-c collinear, b is degree-2 (a bend, not a junction): no turn delay.
    b = RoadGraphBuilder(store_names=False)
    for k, lon in [("a", 8.98), ("b", 9.00), ("c", 9.02)]:
        b.add_node(k, 47.00, lon)
    b.add_road("a", "b", 100, highway="primary")
    b.add_road("b", "c", 100, highway="primary")
    g = b.build()
    assert _T(g).route_index(g.index("a"), g.index("c")).seconds == 200


def test_signal_adds_delay():
    base = _junction()
    sig = _junction(signal=True)
    base_t = _T(base).route_index(base.index("a"), base.index("e")).seconds
    sig_t = _T(sig).route_index(sig.index("a"), sig.index("e")).seconds
    assert sig_t == base_t + TurnCosts().signal


def test_left_costs_more_than_right():
    g = _junction()
    right_t = _T(g).route_index(g.index("a"), g.index("s")).seconds   # east->south
    left_t = _T(g).route_index(g.index("a"), g.index("n")).seconds    # east->north
    assert left_t > right_t             # left turn penalised more
