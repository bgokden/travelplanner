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

from travelplanner.geo import bearing, turn_angle
from travelplanner.graph.road.model import RoadGraph
from travelplanner.graph.road.osm import FERRY_CLASS


@dataclass(frozen=True)
class TurnCosts:
    """Per-turn delay model classified by geometry, with a signal surcharge.

    Defaults follow OSRM-style car costs (seconds). For drive-on-right, turning
    toward the favorable side (right) is cheaper than crossing oncoming traffic
    (left); set drive_on_right=False to mirror for left-hand-traffic countries.
    """
    straight: float = 2.0
    slight: float = 5.0
    favorable: float = 8.0       # gentle turn toward the kerb side
    unfavorable: float = 15.0    # turn across oncoming traffic
    sharp: float = 22.0
    uturn: float = 40.0
    signal: float = 5.0
    drive_on_right: bool = True

    def cost(self, is_uturn: bool, angle: float, via_is_signal: bool) -> float:
        base = self.uturn if is_uturn else self._by_angle(angle)
        return base + (self.signal if via_is_signal else 0.0)

    def _by_angle(self, angle: float) -> float:
        m = abs(angle)
        if m <= 20.0:
            return self.straight
        # A true U-turn is detected topologically (the exit returns to where the
        # approach came from) and charged self.uturn before _by_angle is reached;
        # a near-180 deg angle here is a hairpin onto a *different* road, i.e. a
        # sharp turn -- not a U-turn -- so it must not get the U-turn penalty.
        if m >= 135.0:
            return self.sharp
        if m <= 45.0:
            return self.slight
        right = angle > 0.0          # positive turn_angle = right
        favorable = right if self.drive_on_right else not right
        return self.favorable if favorable else self.unfavorable


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
                        turn_seconds: float = 0.0, forbidden=None,
                        turn_costs: "TurnCosts | None" = None) -> TurnTopology:
    """Turn edges between consecutive arcs, with U-turn / forbidden handling.

    forbidden: optional set of (in_arc, out_arc) pairs to omit (turn restrictions).
    turn_costs: when given, the per-turn delay is classified by geometry
    (left/right/straight/sharp/U-turn) plus a signal surcharge at signal nodes;
    otherwise the flat uturn_seconds/turn_seconds are used.
    """
    forbidden = forbidden or frozenset()
    in_arcs, out_arcs = _incidence(graph)
    tail, head = graph.tail, graph.head
    signals = getattr(graph, "signal_nodes", frozenset())

    # A turn that boards or alights a ferry is not a road manoeuvre (the crossing
    # time already covers loading), so it carries no turn/junction/U-turn cost.
    arc_class = graph.arc_class
    is_ferry = None
    if arc_class is not None and FERRY_CLASS in graph.class_table:
        ferry_class = graph.class_table.index(FERRY_CLASS)
        is_ferry = [arc_class[a] == ferry_class for a in range(graph.arc_count)]

    arc_bearing = None
    is_junction = None
    if turn_costs is not None:
        lat, lon = graph.latitude, graph.longitude
        arc_bearing = [bearing(lat[tail[a]], lon[tail[a]],
                               lat[head[a]], lon[head[a]])
                       for a in range(graph.arc_count)]
        # A real junction touches >= 3 distinct neighbours; a mid-road node (a
        # bend or geometry point) touches 2 and must not incur a turn delay.
        neighbours = [set() for _ in range(graph.node_count)]
        for a in range(graph.arc_count):
            neighbours[tail[a]].add(head[a])
            neighbours[head[a]].add(tail[a])
        is_junction = [len(s) >= 3 for s in neighbours]

    turn_tail = array("i")
    turn_head = array("i")
    turn_extra = array("i")
    for v in range(graph.node_count):
        via_signal = v in signals
        for a in in_arcs[v]:
            a_from = tail[a]
            for b in out_arcs[v]:
                if (a, b) in forbidden:
                    continue
                is_uturn = head[b] == a_from   # b returns toward a's origin
                if is_ferry is not None and (is_ferry[a] or is_ferry[b]):
                    cost = 0.0       # boarding/alighting a ferry is not a turn
                elif turn_costs is not None:
                    if is_uturn:
                        base = turn_costs.uturn
                    elif is_junction[v]:
                        base = turn_costs._by_angle(
                            turn_angle(arc_bearing[a], arc_bearing[b]))
                    else:
                        base = 0.0   # following the road through a non-junction
                    cost = base + (turn_costs.signal if via_signal else 0.0)
                else:
                    cost = uturn_seconds if is_uturn else turn_seconds
                turn_tail.append(a)
                turn_head.append(b)
                turn_extra.append(int(round(cost)))
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
                         turn_seconds: float = 0.0, forbidden=None,
                         turn_costs: "TurnCosts | None" = None) -> ExpandedRoadGraph:
    """Build the turn-expanded graph from a base RoadGraph (see ExpandedRoadGraph).

    forbidden turns default to the graph's OSM turn restrictions
    (base.restricted_turns) when not given explicitly.
    """
    if forbidden is None:
        forbidden = getattr(base, "restricted_turns", None) or frozenset()
    topo = build_turn_topology(base, uturn_seconds=uturn_seconds,
                               turn_seconds=turn_seconds, forbidden=forbidden,
                               turn_costs=turn_costs)
    lat, lon = base.latitude, base.longitude
    tail, head = base.tail, base.head
    # Match the base graph's narrow dtypes: midpoint coords are float32 (only used
    # for CCH ordering) and the validity columns are 16-bit interned indices, so the
    # turn-expanded graph does not re-widen what the base graph just packed.
    mid_lat = array("f", [(lat[tail[a]] + lat[head[a]]) / 2.0
                          for a in range(base.arc_count)])
    mid_lon = array("f", [(lon[tail[a]] + lon[head[a]]) / 2.0
                          for a in range(base.arc_count)])
    av = base.arc_validity
    validity_a = array("h", (av[a] for a in topo.turn_tail))
    validity_b = array("h", (av[b] for b in topo.turn_head))
    return ExpandedRoadGraph(
        base=base, latitude=mid_lat, longitude=mid_lon,
        tail=topo.turn_tail, head=topo.turn_head, turn_cost=topo.turn_extra,
        validity_a=validity_a, validity_b=validity_b,
        in_arcs=topo.in_arcs, out_arcs=topo.out_arcs)
