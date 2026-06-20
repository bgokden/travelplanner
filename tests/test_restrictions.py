"""Stage 3: OSM turn-restriction resolution + flow into the expanded graph."""

from travelplanner.graph.road.model import RoadGraphBuilder
from travelplanner.graph.road.osm import resolve_restrictions


def test_resolve_no_turn():
    # from-way W1 enters via node V as arc 10; to-way W2 leaves V as arc 20.
    # A single candidate pair identifies the turn unambiguously.
    forbidden = resolve_restrictions(
        [(("W1",), "V", ("W2",), "no_left_turn")],
        arc_into={("W1", "V"): [10]},
        arc_outof={("W2", "V"): [20]},
        out_by_node={5: [20, 21, 22]},
        node_index={"V": 5},
        arc_bearing={10: 0.0, 20: 270.0, 21: 90.0, 22: 180.0})
    assert forbidden == {(10, 20)}


def test_resolve_only_turn_bans_the_others():
    # only_* from arc 10: every out-arc of V except the allowed 20 is forbidden.
    forbidden = resolve_restrictions(
        [(("W1",), "V", ("W2",), "only_left_turn")],
        arc_into={("W1", "V"): [10]},
        arc_outof={("W2", "V"): [20]},
        out_by_node={5: [20, 21, 22]},
        node_index={"V": 5},
        arc_bearing={10: 0.0, 20: 270.0, 21: 90.0, 22: 180.0})
    assert forbidden == {(10, 21), (10, 22)}


def test_resolve_unresolvable_is_skipped():
    # missing arc maps -> no crash, no forbidden pair
    assert resolve_restrictions(
        [(("W1",), "V", ("W2",), "no_left_turn")], {}, {}, {}, {"V": 5}, {}) == set()


def test_no_uturn_keeps_straight_through():
    # Bidirectional way W through interior via V: arc_into has both approaches
    # (A->V=0 heading east, B->V=3 heading west), arc_outof both departures
    # (V->A=1 heading west, V->B=2 heading east). A no_u_turn must forbid only
    # the two reversals, never the straight-through movements.
    forbidden = resolve_restrictions(
        [(("W",), "V", ("W",), "no_u_turn")],
        arc_into={("W", "V"): [0, 3]},
        arc_outof={("W", "V"): [1, 2]},
        out_by_node={5: [1, 2]},
        node_index={"V": 5},
        arc_bearing={0: 90.0, 3: 270.0, 1: 270.0, 2: 90.0})
    assert forbidden == {(0, 1), (3, 2)}        # the two U-turns
    assert (0, 2) not in forbidden and (3, 1) not in forbidden  # straight-throughs


def test_no_left_on_bidirectional_bans_only_matching_approach():
    # from-way W bidirectional (A->V=0 east, B->V=3 west) turning onto to-way C
    # (V->C=9, heading north). Only the approach that physically turns left is
    # forbidden; the opposite approach (a right turn) stays legal.
    forbidden = resolve_restrictions(
        [(("W",), "V", ("C",), "no_left_turn")],
        arc_into={("W", "V"): [0, 3]},
        arc_outof={("C", "V"): [9]},
        out_by_node={5: [1, 2, 9]},
        node_index={"V": 5},
        arc_bearing={0: 90.0, 3: 270.0, 9: 0.0})
    assert (0, 9) in forbidden                  # east approach turning left
    assert (3, 9) not in forbidden              # west approach's right turn legal


def test_no_entry_forbids_every_approach():
    # no_entry legally carries several 'from' members: no approach may enter X.
    forbidden = resolve_restrictions(
        [(("A1", "A2", "A3"), "V", ("X",), "no_entry")],
        arc_into={("A1", "V"): [10], ("A2", "V"): [11], ("A3", "V"): [12]},
        arc_outof={("X", "V"): [20]},
        out_by_node={5: [20]},
        node_index={"V": 5},
        arc_bearing={10: 0.0, 11: 120.0, 12: 240.0, 20: 60.0})
    assert forbidden == {(10, 20), (11, 20), (12, 20)}


def test_no_exit_forbids_every_destination():
    # no_exit legally carries several 'to' members: from A you may not exit to any.
    forbidden = resolve_restrictions(
        [(("A",), "V", ("X1", "X2"), "no_exit")],
        arc_into={("A", "V"): [10]},
        arc_outof={("X1", "V"): [20], ("X2", "V"): [21]},
        out_by_node={5: [20, 21]},
        node_index={"V": 5},
        arc_bearing={10: 0.0, 20: 90.0, 21: 180.0})
    assert forbidden == {(10, 20), (10, 21)}


def test_only_enforced_conservatively_when_geometry_has_no_match():
    # only_left_turn with two candidate approaches but no geometric left (the to-
    # way is straight/U relative to both): the mandatory turn must NOT be dropped;
    # every turn except the to-arc is forbidden from each approach.
    forbidden = resolve_restrictions(
        [(("W",), "V", ("C",), "only_left_turn")],
        arc_into={("W", "V"): [0, 3]},
        arc_outof={("C", "V"): [9]},
        out_by_node={5: [1, 2, 9]},
        node_index={"V": 5},
        arc_bearing={0: 0.0, 3: 180.0, 9: 0.0})
    assert forbidden == {(0, 1), (0, 2), (3, 1), (3, 2)}
    assert (0, 9) not in forbidden and (3, 9) not in forbidden   # to-arc allowed


def test_no_straight_on_does_not_forbid_a_non_straight_turn():
    # no_straight_on onto a perpendicular to-way: neither approach is straight, so
    # nothing may be forbidden (must not ban an arbitrary 90-degree turn).
    forbidden = resolve_restrictions(
        [(("W",), "V", ("C",), "no_straight_on")],
        arc_into={("W", "V"): [0, 3]},
        arc_outof={("C", "V"): [9]},
        out_by_node={5: [9]},
        node_index={"V": 5},
        arc_bearing={0: 0.0, 3: 180.0, 9: 90.0})
    assert forbidden == set()


def test_no_straight_on_symmetric_corridor_bans_both_directions():
    # via interior to a bidirectional from-way W and bidirectional to-way C on a
    # straight corridor: BOTH straight-throughs are forbidden, not just one.
    forbidden = resolve_restrictions(
        [(("W",), "V", ("C",), "no_straight_on")],
        arc_into={("W", "V"): [0, 3]},
        arc_outof={("C", "V"): [1, 2]},
        out_by_node={5: [1, 2]},
        node_index={"V": 5},
        arc_bearing={0: 90.0, 3: 270.0, 1: 90.0, 2: 270.0})
    assert forbidden == {(0, 1), (3, 2)}


def test_only_with_absent_to_way_bans_all_other_turns():
    # only_X but the to-way is not in the driving graph (filtered/not loaded): the
    # one allowed turn is unavailable, so every turn from the approach is banned.
    forbidden = resolve_restrictions(
        [(("W",), "V", ("GONE",), "only_straight_on")],
        arc_into={("W", "V"): [10]},
        arc_outof={},                           # to-way absent
        out_by_node={5: [20, 21, 22]},
        node_index={"V": 5},
        arc_bearing={10: 0.0, 20: 90.0, 21: 180.0, 22: 270.0})
    assert forbidden == {(10, 20), (10, 21), (10, 22)}


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
