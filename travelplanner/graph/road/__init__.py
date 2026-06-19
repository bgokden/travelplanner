"""travelplanner.graph.road: CCH road engine (Phase 1).

The lightweight pieces (RoadGraph/RoadGraphBuilder, and the OSM parser in
.osm) have no heavy dependencies. The CCH engine (CCHRoadRouter and friends)
requires the `road` extra (routingkit-cch); it is imported lazily so that
importing the model or parser does not require routingkit to be installed.
"""

from travelplanner.graph.road.model import RoadGraph, RoadGraphBuilder

__all__ = [
    "RoadGraph",
    "RoadGraphBuilder",
    "CCHRoadRouter",
    "CustomizedRoad",
    "RoadPath",
    "INF",
]

_LAZY = {"CCHRoadRouter", "CustomizedRoad", "RoadPath", "INF"}


def __getattr__(name: str):
    if name in _LAZY:
        from travelplanner.graph.road import router
        return getattr(router, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
