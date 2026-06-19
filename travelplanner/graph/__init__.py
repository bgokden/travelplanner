"""travelplanner.graph: v2 multimodal graph contracts (Phase 0).

Schema, validity model, and query interface shared by the road engine (CCH)
and the scheduled engine (RAPTOR/CSA). See tmp/ARCHITECTURE_V2.md.
"""

from travelplanner.graph.query import Objective, TravelQuery
from travelplanner.graph.schema import (
    Connection,
    Edge,
    MultimodalGraph,
    Node,
    NodeType,
)
from travelplanner.graph.validity import ALWAYS, ServiceCalendar, Validity

__all__ = [
    "NodeType",
    "Node",
    "Connection",
    "Edge",
    "MultimodalGraph",
    "ServiceCalendar",
    "Validity",
    "ALWAYS",
    "Objective",
    "TravelQuery",
]
