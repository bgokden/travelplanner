"""travelplanner.graph.road: CCH road engine (Phase 1).

The lightweight pieces (RoadGraph/RoadGraphBuilder, and the OSM parser in
.osm) are pure-Python. The CCH engine (CCHRoadRouter and friends) is backed by
routingkit-cch (a core dependency); it is imported lazily so that importing the
model or parser does not pull in the native extension until it is needed.
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
