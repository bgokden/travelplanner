"""Turn expansion (Stage 1): build the turn topology of a road graph.

To make routing turn-aware, the routing nodes become the directed ARCS of the
base graph and the edges become legal TURNS between them (see TURN_AWARE_DESIGN).
This module computes that turn topology -- arc incidence per node and the turn
edges with their extra cost -- as a pure function, independent of the CCH engine.

A turn at node v goes from an in-arc `a` (..->v) to an out-arc `b` (v->..).
U-turns (b returns to where a came from) are kept but heavily penalized so they
are used only as a last resort (e.g. dead ends); explicitly forbidden turns
(from OSM restrictions, a later stage) are omitted entirely.
"""

from array import array
from dataclasses import dataclass

from travelplanner.graph.road.model import RoadGraph


@dataclass(frozen=True)
class TurnTopology:
    in_arcs: list[list[int]]      # per base node: arc indices entering it
    out_arcs: list[list[int]]     # per base node: arc indices leaving it
    turn_tail: array              # per turn edge: the in-arc (expanded tail)
    turn_head: array              # per turn edge: the out-arc (expanded head)
    turn_extra: array             # per turn edge: extra seconds (turn/U-turn cost)

    @property
    def turn_count(self) -> int:
        return len(self.turn_tail)


def _incidence(graph) -> tuple[list[list[int]], list[list[int]]]:
    in_arcs: list[list[int]] = [[] for _ in range(graph.node_count)]
    out_arcs: list[list[int]] = [[] for _ in range(graph.node_count)]
    tail, head = graph.tail, graph.head
    for arc in range(graph.arc_count):
        out_arcs[tail[arc]].append(arc)
        in_arcs[head[arc]].append(arc)
    return in_arcs, out_arcs


def build_turn_topology(graph, *, uturn_seconds: float = 120.0,
                        turn_seconds: float = 0.0,
                        forbidden=None) -> TurnTopology:
    """Turn edges between consecutive arcs, with U-turn / forbidden handling.

    forbidden: optional set of (in_arc, out_arc) pairs to omit (turn restrictions).
    """
    forbidden = forbidden or frozenset()
    in_arcs, out_arcs = _incidence(graph)
    tail, head = graph.tail, graph.head

    turn_tail = array("i")
    turn_head = array("i")
    turn_extra = array("i")
    for v in range(graph.node_count):
        for a in in_arcs[v]:
            a_from = tail[a]
            for b in out_arcs[v]:
                if (a, b) in forbidden:
                    continue
                is_uturn = head[b] == a_from   # b returns toward a's origin
                turn_tail.append(a)
                turn_head.append(b)
                turn_extra.append(int(round(uturn_seconds if is_uturn
                                            else turn_seconds)))
    return TurnTopology(in_arcs, out_arcs, turn_tail, turn_head, turn_extra)


@dataclass(frozen=True)
class ExpandedRoadGraph:
    """Turn-expanded view of a base graph: nodes = base arcs, edges = turns.

    Routed by the CCH engine (ExpandedCCHRoadRouter). Per-node data (travel time,
    class) comes from the base arc the node represents; per-edge data (turn_cost,
    the validity of both the entered and exited arc) lives on the turn edges.
    """
    base: RoadGraph
    latitude: array        # per expanded node: base arc midpoint (for CCH order)
    longitude: array
    tail: array            # per turn edge: in-arc (expanded tail)
    head: array            # per turn edge: out-arc (expanded head)
    turn_cost: array       # per turn edge: fixed junction/turn delay (seconds)
    validity_a: array      # per turn edge: validity index of the traversed arc (tail)
    validity_b: array      # per turn edge: validity index of the entered arc (head)
    in_arcs: list          # per base node: arc indices entering it
    out_arcs: list         # per base node: arc indices leaving it

    @property
    def node_count(self) -> int:
        return self.base.arc_count

    @property
    def edge_count(self) -> int:
        return len(self.tail)


def build_expanded_graph(base: RoadGraph, *, uturn_seconds: float = 120.0,
                         turn_seconds: float = 0.0,
                         forbidden=None) -> ExpandedRoadGraph:
    """Build the turn-expanded graph from a base RoadGraph (see ExpandedRoadGraph)."""
    topo = build_turn_topology(base, uturn_seconds=uturn_seconds,
                               turn_seconds=turn_seconds, forbidden=forbidden)
    lat, lon = base.latitude, base.longitude
    tail, head = base.tail, base.head
    mid_lat = array("d", [(lat[tail[a]] + lat[head[a]]) / 2.0
                          for a in range(base.arc_count)])
    mid_lon = array("d", [(lon[tail[a]] + lon[head[a]]) / 2.0
                          for a in range(base.arc_count)])
    av = base.arc_validity
    validity_a = array("i", (av[a] for a in topo.turn_tail))
    validity_b = array("i", (av[b] for b in topo.turn_head))
    return ExpandedRoadGraph(
        base=base, latitude=mid_lat, longitude=mid_lon,
        tail=topo.turn_tail, head=topo.turn_head, turn_cost=topo.turn_extra,
        validity_a=validity_a, validity_b=validity_b,
        in_arcs=topo.in_arcs, out_arcs=topo.out_arcs)
