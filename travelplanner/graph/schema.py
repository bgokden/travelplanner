"""Multimodal graph schema: nodes, edges, and the graph container.

This is the contract the road engine (CCH, Phase 1) and the scheduled engine
(RAPTOR/CSA, Phase 2) both consume. An edge is either STATIC (road/walk/
transfer: a fixed traversal time) or SCHEDULED (transit: a timetable of
connections). Every edge carries a mode label and a validity predicate.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum

from travelplanner.models import CostLevel, Mode
from travelplanner.graph.validity import ALWAYS, Validity


class NodeType(Enum):
    AIRPORT = "airport"
    RAIL_STATION = "rail_station"
    FERRY_TERMINAL = "ferry_terminal"
    ROAD_JUNCTION = "road_junction"
    PLACE = "place"  # a resolved user endpoint: hotel, city, landmark, etc.


@dataclass(frozen=True)
class Node:
    id: str
    type: NodeType
    lat: float
    lon: float
    name: str = ""


@dataclass(frozen=True)
class Connection:
    """One scheduled vehicle segment on a transit edge."""

    depart: datetime
    arrive: datetime
    cost_level: CostLevel = CostLevel.MEDIUM
    trip_id: str = ""


@dataclass(frozen=True)
class Edge:
    id: str
    from_node: str
    to_node: str
    mode: Mode
    distance_km: float = 0.0
    validity: Validity = ALWAYS
    cost_level: CostLevel = CostLevel.MEDIUM
    static_seconds: float | None = None
    connections: tuple[Connection, ...] = ()

    def __post_init__(self) -> None:
        has_static = self.static_seconds is not None
        has_schedule = bool(self.connections)
        if has_static == has_schedule:
            raise ValueError(
                f"Edge {self.id!r} must be exactly one of STATIC "
                f"(static_seconds) or SCHEDULED (connections)."
            )

    @property
    def is_scheduled(self) -> bool:
        return bool(self.connections)

    def earliest_arrival(self, departure: datetime,
                         conditions: frozenset[str] = frozenset()
                         ) -> datetime | None:
        """Earliest arrival at to_node if departing from_node no earlier than
        `departure`, or None if the edge is inactive or unreachable in time.
        """
        if not self.validity.is_active(departure.date(), conditions):
            return None
        if self.is_scheduled:
            reachable = [c for c in self.connections if c.depart >= departure]
            if not reachable:
                return None
            return min(reachable, key=lambda c: c.arrive).arrive
        return departure + timedelta(seconds=self.static_seconds or 0.0)


@dataclass
class MultimodalGraph:
    nodes: dict[str, Node] = field(default_factory=dict)
    edges: dict[str, Edge] = field(default_factory=dict)
    _adjacency: dict[str, list[str]] = field(default_factory=dict)

    def add_node(self, node: Node) -> Node:
        self.nodes[node.id] = node
        self._adjacency.setdefault(node.id, [])
        return node

    def add_edge(self, edge: Edge) -> Edge:
        if edge.from_node not in self.nodes:
            raise KeyError(f"Unknown from_node {edge.from_node!r}")
        if edge.to_node not in self.nodes:
            raise KeyError(f"Unknown to_node {edge.to_node!r}")
        self.edges[edge.id] = edge
        self._adjacency.setdefault(edge.from_node, []).append(edge.id)
        return edge

    def out_edges(self, node_id: str) -> list[Edge]:
        return [self.edges[eid] for eid in self._adjacency.get(node_id, [])]

    def active_out_edges(self, node_id: str, day,
                         conditions: frozenset[str] = frozenset()
                         ) -> list[Edge]:
        """Out-edges usable on `day` under `conditions` (validity-filtered)."""
        return [e for e in self.out_edges(node_id)
                if e.validity.is_active(day, conditions)]
