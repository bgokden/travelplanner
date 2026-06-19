"""Customizable Contraction Hierarchies road engine.

Three phases, mirroring CRP/CCH:
  1. Preprocessing (once): topology-only contraction order + CCH structure.
  2. Customization (per date/conditions): build a weight metric where edges
     that are inactive (out of season, closed, condition unmet) get INF.
  3. Query: shortest path on the customized metric.

Seasonal/conditional changes never touch phase 1. A full re-customization
rebuilds the metric; a partial update flips individual arcs in place
(e.g. open/close a single pass) without rebuilding.
"""

from dataclasses import dataclass
from datetime import date

import routingkit_cch as rk

from travelplanner.graph.road.model import RoadGraph

# Large sentinel for a closed/unusable arc. Chosen so a single closed arc
# dominates any realistic route yet two of them stay within int range.
INF = 1_000_000_000


@dataclass(frozen=True)
class RoadPath:
    seconds: int
    node_keys: list[str]
    arc_indices: list[int]


class CCHRoadRouter:
    """Phase 1: metric-independent preprocessing over a RoadGraph."""

    def __init__(self, graph: RoadGraph) -> None:
        self.graph = graph
        # routingkit needs plain lists; build them once (freed after setup).
        tail = list(graph.tail)
        head = list(graph.head)
        order = rk.compute_order_inertial(
            graph.node_count, tail, head,
            list(graph.latitude), list(graph.longitude),
        )
        self._cch = rk.CCH(order, tail, head, False)
        self._updater = rk.CCHMetricPartialUpdater(self._cch)

    def _weights_for(self, day: date, conditions: frozenset[str]) -> list[int]:
        # Evaluate each distinct validity once, then map arcs by their interned
        # index (cheap at country scale where the table is tiny).
        active = [v.is_active(day, conditions)
                  for v in self.graph.validity_table]
        base = self.graph.base_seconds
        vidx = self.graph.arc_validity
        return [base[i] if active[vidx[i]] else INF for i in range(len(base))]

    def customize(self, day: date,
                  conditions: frozenset[str] = frozenset()) -> "CustomizedRoad":
        """Phase 2: build a queryable metric for a given day and conditions."""
        weights = self._weights_for(day, conditions)
        metric = rk.CCHMetric(self._cch, weights)
        return CustomizedRoad(self, metric, weights)


class CustomizedRoad:
    """A customized metric you can query, and update arc-by-arc."""

    def __init__(self, router: CCHRoadRouter, metric, weights: list[int]) -> None:
        self._router = router
        self._metric = metric
        self._weights = list(weights)
        self._query = rk.CCHQuery(metric)

    def route(self, from_key: str, to_key: str) -> RoadPath | None:
        """Phase 3: shortest path, or None if unreachable / only via closed arcs."""
        g = self._router.graph
        res = self._query.run(g.index(from_key), g.index(to_key))
        if res.distance is None or res.distance >= INF:
            return None
        return RoadPath(
            seconds=res.distance,
            node_keys=[g.key(i) for i in res.node_path],
            arc_indices=list(res.arc_path),
        )

    def update_arcs(self, arc_weights: dict[int, int]) -> None:
        """Partial re-customization: set new weights on specific arcs.

        The active query must be released before the updater runs (the engine
        forbids updating a metric with a live query), so we drop and rebuild it.
        """
        if not arc_weights:
            return
        self._query = None  # release the only reference so the update is allowed
        self._router._updater.apply(self._metric, arc_weights)
        for arc, w in arc_weights.items():
            self._weights[arc] = w
        self._query = rk.CCHQuery(self._metric)

    def close_named(self, name: str) -> None:
        """Close every arc carrying `name` (e.g. a pass)."""
        self.update_arcs({a: INF for a in self._router.graph.arcs_by_name(name)})

    def open_named(self, name: str) -> None:
        """Reopen every arc carrying `name`, restoring its base weight."""
        base = self._router.graph.base_seconds
        self.update_arcs(
            {a: base[a] for a in self._router.graph.arcs_by_name(name)}
        )
