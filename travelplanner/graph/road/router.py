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

# Bound on the per-router cache of customized metrics (one per distinct
# day/conditions); FIFO eviction keeps memory bounded for long-lived processes.
_CUSTOMIZED_CACHE_MAX = 8


@dataclass(frozen=True)
class RoadPath:
    seconds: int
    node_indices: list[int]
    node_keys: list
    arc_indices: list[int]


class CCHRoadRouter:
    """Phase 1: metric-independent preprocessing over a RoadGraph."""

    def __init__(self, graph: RoadGraph, order: list[int] | None = None) -> None:
        if graph.node_count == 0:
            # routingkit's order/CCH construction segfaults on a 0-node graph;
            # reject an empty graph here with a clear error instead.
            raise ValueError("cannot route an empty graph (no nodes)")
        self.graph = graph
        self._node_grid = None
        # routingkit needs plain lists; build them once (freed after setup).
        tail = list(graph.tail)
        head = list(graph.head)
        # The contraction order is the expensive part of preprocessing; a caller
        # that persisted it (offline build) can pass it in to skip recomputation.
        if order is None:
            order = rk.compute_order_inertial(
                graph.node_count, tail, head,
                list(graph.latitude), list(graph.longitude),
            )
        self.order = list(order)
        self._cch = rk.CCH(self.order, tail, head, False)
        self._updater = rk.CCHMetricPartialUpdater(self._cch)
        self._customized_cache: dict = {}

    @property
    def node_grid(self):
        """Spatial index for nearest-node snapping (built once, on first use)."""
        if self._node_grid is None:
            from travelplanner.graph.road.spatial import NodeGrid
            self._node_grid = NodeGrid.build(self.graph.latitude,
                                             self.graph.longitude)
        return self._node_grid

    def _class_multipliers(self, depart_at, speed_model):
        """Per-class time multiplier for the active/given speed model (or None)."""
        table = self.graph.class_table
        if not table or self.graph.arc_class is None:
            return None
        from travelplanner.speed import get_speed_model
        model = speed_model or get_speed_model()
        return [model(cls or None, depart_at) for cls in table]

    def _weights_for(self, day: date, conditions: frozenset[str],
                     class_mult) -> list[int]:
        # Evaluate each distinct validity (and class multiplier) once, then map
        # arcs by their interned index (cheap; the tables are tiny).
        active = [v.is_active(day, conditions)
                  for v in self.graph.validity_table]
        base = self.graph.base_seconds
        vidx = self.graph.arc_validity
        cidx = self.graph.arc_class
        if class_mult is None or cidx is None:
            return [base[i] if active[vidx[i]] else INF for i in range(len(base))]
        return [round(base[i] * class_mult[cidx[i]]) if active[vidx[i]] else INF
                for i in range(len(base))]

    def customize(self, day: date, conditions: frozenset[str] = frozenset(), *,
                  depart_at=None, speed_model=None) -> "CustomizedRoad":
        """Phase 2: build a fresh queryable metric for a day and conditions.

        The speed model (active default: average; pass speed_model to override)
        scales each arc's free-flow time by a per-class, departure-time-dependent
        multiplier. Each call rebuilds the metric (~milliseconds at country
        scale). Use this when you intend to mutate the result
        (update_arcs/close_named/open_named); for repeated read-only queries,
        prefer customized(), which caches and reuses the metric.
        """
        class_mult = self._class_multipliers(depart_at, speed_model)
        weights = self._weights_for(day, conditions, class_mult)
        metric = rk.CCHMetric(self._cch, weights)
        return CustomizedRoad(self, metric, weights)

    def customized(self, day: date, conditions: frozenset[str] = frozenset(), *,
                   depart_at=None, speed_model=None) -> "CustomizedRoad":
        """A cached, read-only CustomizedRoad for (day, conditions, model, hour).

        Reuses the metric across queries (customize() rebuilds it each call), so
        many same-day lookups (e.g. a driving matrix) pay the build once. Do not
        mutate the returned object; use customize() when you need a mutable one.
        """
        from travelplanner.speed import get_speed_model
        model = speed_model or get_speed_model()
        bucket = None if depart_at is None else depart_at.hour
        key = (day, conditions, model, bucket)
        cache = self._customized_cache
        road = cache.get(key)
        if road is None:
            road = self.customize(day, conditions, depart_at=depart_at,
                                  speed_model=model)
            cache[key] = road
            if len(cache) > _CUSTOMIZED_CACHE_MAX:
                cache.pop(next(iter(cache)))  # FIFO evict oldest insertion
        return road


class CustomizedRoad:
    """A customized metric you can query, and update arc-by-arc."""

    def __init__(self, router: CCHRoadRouter, metric, weights: list[int]) -> None:
        self._router = router
        self._metric = metric
        self._weights = list(weights)
        self._query = rk.CCHQuery(metric)

    def route(self, from_key, to_key) -> RoadPath | None:
        """Shortest path between two node keys (resolves keys to indices)."""
        g = self._router.graph
        return self.route_index(g.index(from_key), g.index(to_key))

    def route_index(self, from_index: int, to_index: int) -> RoadPath | None:
        """Phase 3: shortest path by node index, or None if unreachable.

        The index-based entry point avoids the key -> index lookup, so callers
        that already hold node indices (e.g. coordinate snapping) never build the
        reverse key map.
        """
        g = self._router.graph
        res = self._query.run(from_index, to_index)
        if res.distance is None or res.distance >= INF:
            return None
        node_indices = list(res.node_path)
        return RoadPath(
            seconds=res.distance,
            node_indices=node_indices,
            node_keys=[g.key(i) for i in node_indices],
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
