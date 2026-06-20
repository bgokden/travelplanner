"""Stage 3: OSM turn-restriction resolution + flow into the expanded graph."""

from travelplanner.graph.road.model import RoadGraphBuilder
from travelplanner.graph.road.osm import resolve_restrictions


def test_resolve_no_turn():
    # from-way W1 enters via node V as arc 10; to-way W2 leaves V as arc 20.
    forbidden = resolve_restrictions(
        [("W1", "V", "W2", "no")],
        arc_into={("W1", "V"): [10]},
        arc_outof={("W2", "V"): [20]},
        out_by_node={5: [20, 21, 22]},
        node_index={"V": 5})
    assert forbidden == {(10, 20)}


def test_resolve_only_turn_bans_the_others():
    # only_* from arc 10: every out-arc of V except 20 is forbidden.
    forbidden = resolve_restrictions(
        [("W1", "V", "W2", "only")],
        arc_into={("W1", "V"): [10]},
        arc_outof={("W2", "V"): [20]},
        out_by_node={5: [20, 21, 22]},
        node_index={"V": 5})
    assert forbidden == {(10, 21), (10, 22)}


def test_resolve_unresolvable_is_skipped():
    # missing arc maps -> no crash, no forbidden pair
    assert resolve_restrictions(
        [("W1", "V", "W2", "no")], {}, {}, {}, {"V": 5}) == set()


def test_restricted_turns_flow_into_expansion():
    # A '+' junction; forbid the a->C ... C->e straight turn via restricted_turns.
    b = RoadGraphBuilder(store_names=False)
    for k, (lat, lon) in {"C": (47.0, 9.0), "a": (47.0, 8.98),
                          "e": (47.0, 9.02), "n": (47.02, 9.0)}.items():
        b.add_node(k, lat, lon)
    b.add_road("a", "C", 100)
    b.add_road("C", "e", 100)
    b.add_road("C", "n", 100)
    g = b.build()
    aC = next(i for i in range(g.arc_count)
              if g.tail[i] == g.index("a") and g.head[i] == g.index("C"))
    Ce = next(i for i in range(g.arc_count)
              if g.tail[i] == g.index("C") and g.head[i] == g.index("e"))

    # build_expanded_graph defaults forbidden to graph.restricted_turns
    import dataclasses
    g2 = dataclasses.replace(g, restricted_turns=frozenset({(aC, Ce)}))
    from travelplanner.graph.road.turns import build_expanded_graph
    exp = build_expanded_graph(g2, turn_seconds=0)
    pairs = set(zip(exp.tail, exp.head))
    assert (aC, Ce) not in pairs               # restricted turn omitted
    assert (aC, next(i for i in range(g.arc_count)
                     if g.tail[i] == g.index("C")
                     and g.head[i] == g.index("n"))) in pairs


def test_builder_set_restricted_turns():
    b = RoadGraphBuilder(store_names=False)
    b.add_node("a", 47.0, 9.0)
    b.add_node("b", 47.0, 9.01)
    b.add_road("a", "b", 100)
    b.set_restricted_turns({(0, 1)})
    g = b.build()
    assert g.restricted_turns == frozenset({(0, 1)})
