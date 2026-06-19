"""Turn-aware routing over a turn-expanded graph (Stage 2).

ExpandedCCHRoadRouter runs the CCH engine over an ExpandedRoadGraph (nodes = base
arcs, edges = legal turns). It mirrors CCHRoadRouter's interface -- .graph (the
base graph, for snapping/geometry), .node_grid, .customize/.customized returning
an object whose .route_index(from_node, to_node) yields a base-node RoadPath --
so drive()/drive_route() use it unchanged.

Weight of a turn edge a->b = round(time(a) * class_mult[class(a)]) + turn_cost,
active only when BOTH arc a and arc b are active (you can't traverse or enter a
closed road). A query snaps origin/dest to base nodes and takes the min over
(out-arc of origin) x (in-arc of dest), with a single-arc short-circuit.
"""

from datetime import date

import routingkit_cch as rk

from travelplanner.graph.road.router import INF, RoadPath
from travelplanner.graph.road.turns import ExpandedRoadGraph

_CUSTOMIZED_CACHE_MAX = 8


class ExpandedCCHRoadRouter:
    def __init__(self, expanded: ExpandedRoadGraph, order: list[int] | None = None) -> None:
        self.expanded = expanded
        self.graph = expanded.base          # snapping/geometry use the base graph
        self._node_grid = None
        tail = list(expanded.tail)
        head = list(expanded.head)
        if order is None:
            order = rk.compute_order_inertial(
                expanded.node_count, tail, head,
                list(expanded.latitude), list(expanded.longitude))
        self.order = list(order)
        self._cch = rk.CCH(self.order, tail, head, False)
        self._customized_cache: dict = {}

    @property
    def node_grid(self):
        if self._node_grid is None:
            from travelplanner.graph.road.spatial import NodeGrid
            self._node_grid = NodeGrid.build(self.graph.latitude,
                                             self.graph.longitude)
        return self._node_grid

    def _weights_for(self, day: date, conditions, class_mult) -> list[int]:
        base, exp = self.graph, self.expanded
        active = [v.is_active(day, conditions) for v in base.validity_table]
        bsec = base.base_seconds
        bclass = base.arc_class
        et = exp.tail
        tc = exp.turn_cost
        va, vb = exp.validity_a, exp.validity_b
        if class_mult is None or bclass is None:
            return [bsec[et[i]] + tc[i] if active[va[i]] and active[vb[i]] else INF
                    for i in range(len(et))]
        return [round(bsec[et[i]] * class_mult[bclass[et[i]]]) + tc[i]
                if active[va[i]] and active[vb[i]] else INF
                for i in range(len(et))]

    def _class_multipliers(self, depart_at, speed_model):
        table = self.graph.class_table
        if not table or self.graph.arc_class is None:
            return None
        from travelplanner.speed import get_speed_model
        model = speed_model or get_speed_model()
        return [model(cls or None, depart_at) for cls in table]

    def _arc_times(self, class_mult) -> list[int]:
        """Per base arc: travel time after the speed multiplier (matches the
        time component of the turn-edge weights, so the final arc added at query
        time is consistent)."""
        base = self.graph
        bsec, bclass = base.base_seconds, base.arc_class
        if class_mult is None or bclass is None:
            return list(bsec)
        return [round(bsec[a] * class_mult[bclass[a]])
                for a in range(base.arc_count)]

    def customize(self, day: date, conditions: frozenset = frozenset(), *,
                  depart_at=None, speed_model=None) -> "ExpandedCustomized":
        class_mult = self._class_multipliers(depart_at, speed_model)
        weights = self._weights_for(day, conditions, class_mult)
        active = [v.is_active(day, conditions) for v in self.graph.validity_table]
        metric = rk.CCHMetric(self._cch, weights)
        return ExpandedCustomized(self, metric, active, self._arc_times(class_mult))

    def customized(self, day: date, conditions: frozenset = frozenset(), *,
                   depart_at=None, speed_model=None) -> "ExpandedCustomized":
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
                cache.pop(next(iter(cache)))
        return road


class ExpandedCustomized:
    def __init__(self, router: ExpandedCCHRoadRouter, metric, active,
                 arc_time) -> None:
        self._router = router
        self._query = rk.CCHQuery(metric)
        self._active = active            # per-validity-index usability (for s==t)
        self._arc_time = arc_time        # per base arc: multiplied travel time

    def _arc_active(self, arc) -> bool:
        return self._active[self._router.graph.arc_validity[arc]]

    def _dist(self, s: int, t: int):
        res = self._query.run(s, t)
        if res.distance is None or res.distance >= INF:
            return None, None
        return res.distance, list(res.node_path)

    def route(self, from_key, to_key) -> RoadPath | None:
        """Turn-aware shortest path between two base node keys (resolves to
        indices). Mirrors CustomizedRoad.route so a connector is router-agnostic."""
        g = self._router.graph
        return self.route_index(g.index(from_key), g.index(to_key))

    def route_index(self, from_node: int, to_node: int) -> RoadPath | None:
        """Turn-aware shortest path between two base node indices."""
        exp = self._router.expanded
        base = self._router.graph
        arc_time = self._arc_time
        best_total, best_arcs = None, None
        for s in exp.out_arcs[from_node]:
            for t in exp.in_arcs[to_node]:
                if s == t:                       # single arc origin->dest directly
                    if not self._arc_active(s):
                        continue
                    total, arcs = arc_time[t], [s]
                else:
                    dist, path = self._dist(s, t)
                    if dist is None:
                        continue
                    total, arcs = dist + arc_time[t], path
                if best_total is None or total < best_total:
                    best_total, best_arcs = total, arcs
        if best_arcs is None:
            return None
        node_path = [base.tail[best_arcs[0]]] + [base.head[a] for a in best_arcs]
        return RoadPath(seconds=best_total, node_indices=node_path,
                        node_keys=[base.key(i) for i in node_path],
                        arc_indices=best_arcs)
