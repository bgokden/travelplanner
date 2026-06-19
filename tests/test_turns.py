"""Turn topology (Stage 1): incidence, turn edges, U-turn + forbidden handling."""

from travelplanner.graph.road.model import RoadGraphBuilder
from travelplanner.graph.road.turns import build_turn_topology


def _cross():
    # A '+' intersection: centre C with four bidirectional spokes.
    b = RoadGraphBuilder(store_names=False)
    b.add_node("C", 47.00, 9.00)
    b.add_node("N", 47.01, 9.00)
    b.add_node("E", 47.00, 9.01)
    b.add_node("S", 46.99, 9.00)
    b.add_node("W", 47.00, 8.99)
    for spoke in ("N", "E", "S", "W"):
        b.add_road("C", spoke, 60)   # two arcs each (C->spoke, spoke->C)
    return b.build()


def _arc(graph, frm, to):
    fi, ti = graph.index(frm), graph.index(to)
    for arc in range(graph.arc_count):
        if graph.tail[arc] == fi and graph.head[arc] == ti:
            return arc
    raise KeyError((frm, to))


def test_incidence():
    g = _cross()
    topo = build_turn_topology(g)
    c = g.index("C")
    assert len(topo.in_arcs[c]) == 4 and len(topo.out_arcs[c]) == 4
    # a leaf node has one in and one out arc
    assert len(topo.in_arcs[g.index("N")]) == 1


def test_turn_count_and_uturns():
    g = _cross()
    topo = build_turn_topology(g, uturn_seconds=120, turn_seconds=0)
    # At C: 4 in-arcs x 4 out-arcs = 16 turns; leaves add a U-turn each (1x1).
    # Total turns at C = 16 (incl. 4 U-turns); at each leaf = 1 U-turn.
    assert topo.turn_count == 16 + 4
    uturns = sum(1 for e in topo.turn_extra if e == 120)
    assert uturns == 4 + 4            # 4 at the centre, 1 at each of 4 leaves


def test_uturn_penalty_applied():
    g = _cross()
    topo = build_turn_topology(g, uturn_seconds=99, turn_seconds=3)
    n_to_c = _arc(g, "N", "C")
    c_to_n = _arc(g, "C", "N")
    c_to_e = _arc(g, "C", "E")
    extra = {(t, h): x for t, h, x in
             zip(topo.turn_tail, topo.turn_head, topo.turn_extra)}
    assert extra[(n_to_c, c_to_n)] == 99    # N->C->N is a U-turn
    assert extra[(n_to_c, c_to_e)] == 3     # N->C->E is a normal turn


def test_forbidden_turn_omitted():
    g = _cross()
    n_to_c = _arc(g, "N", "C")
    c_to_e = _arc(g, "C", "E")
    topo = build_turn_topology(g, forbidden={(n_to_c, c_to_e)})
    pairs = set(zip(topo.turn_tail, topo.turn_head))
    assert (n_to_c, c_to_e) not in pairs
    assert topo.turn_count == 16 + 4 - 1
