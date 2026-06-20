"""travelplanner.graph.coupling: door-to-door planner (Phase 3).

Couples the road layer (access/egress/transfers) with the scheduled layer
(line-haul) via phased decomposition. The geometric connector is dependency-
light; the CCH connector wires the Phase 1 engine.
"""

from travelplanner.graph.coupling.connector import (
    AccessLeg,
    CCHConnector,
    GeometricConnector,
    RoadConnector,
    SplitConnector,
)
from travelplanner.graph.coupling.planner import plan, plan_multi

__all__ = [
    "plan",
    "plan_multi",
    "AccessLeg",
    "RoadConnector",
    "GeometricConnector",
    "CCHConnector",
    "SplitConnector",
]
