"""Connection Scan Algorithm: earliest-arrival routing on a Timetable.

A single forward scan over departure-sorted connections. Trip-based: once a
vehicle run is boarded it can be ridden without a transfer penalty; boarding a
*different* run after alighting requires the stop's minimum change time.
Multi-source by construction (seed any number of (stop, ready-time) pairs),
which is what the road/transit coupling in Phase 3 needs.

Footpaths are assumed transitively closed (a single relaxation step is applied
per stop improvement).
"""

import heapq
from dataclasses import dataclass
from datetime import datetime, timedelta

from travelplanner.models import CostLevel, Mode
from travelplanner.graph.scheduled.model import Connection, Footpath, Timetable

# Longest walking chain kept when transitively closing footpaths. Changing
# vehicles on foot over more than this is not a realistic transfer, and the cap
# bounds the closure in a dense feed whose short transfers chain across a city
# (without it the all-pairs closure explodes).
_MAX_TRANSFER_CHAIN = timedelta(minutes=30)


@dataclass(frozen=True)
class JourneyLeg:
    mode: Mode
    from_stop: str
    to_stop: str
    departure: datetime
    arrival: datetime
    cost_level: CostLevel
    trip_id: str | None  # None for a walking/footpath leg


@dataclass(frozen=True)
class Journey:
    legs: tuple[JourneyLeg, ...]

    @property
    def depart(self) -> datetime:
        return self.legs[0].departure

    @property
    def arrive(self) -> datetime:
        return self.legs[-1].arrival

    @property
    def transfers(self) -> int:
        vehicles = sum(1 for leg in self.legs if leg.trip_id is not None)
        return max(0, vehicles - 1)

    @property
    def cost_level(self) -> CostLevel:
        ranks = [leg.cost_level.rank for leg in self.legs
                 if leg.trip_id is not None]
        return CostLevel.from_rank(max(ranks)) if ranks else CostLevel.LOW


class ConnectionScan:
    def __init__(self, timetable: Timetable,
                 horizon: timedelta = timedelta(days=2)) -> None:
        self.tt = timetable
        self.horizon = horizon
        # Transitively close footpaths so a single relaxation step per stop
        # improvement is sufficient (the scan never re-relaxes a stop reached only
        # by walking).
        self._fp_from = self._closed_footpaths(timetable)
        # Materialized connections, memoized by (t0, t_end, conditions). One plan
        # runs several scans (one per line-haul mode set, plus the egress query)
        # over the SAME window, so materializing once and reusing it avoids
        # repeating the expensive build on a dense feed.
        self._conn_cache: dict = {}

    @staticmethod
    def _closed_footpaths(timetable: Timetable) -> dict[str, list[Footpath]]:
        # Shortest walking time from each stop to the others reachable on foot, so
        # the scan needs only a single relaxation step per improvement. Computed as
        # a bounded Dijkstra from each stop over the (sparse) footpath graph: the
        # footpaths form small local clusters, so this is far cheaper than an
        # all-pairs Floyd-Warshall (O(V^3), which hangs on a dense urban feed).
        # Chains longer than _MAX_TRANSFER_CHAIN are dropped (an unrealistic walk to
        # transfer), which also bounds the work when transfers chain across a city.
        adj: dict[str, list[tuple[str, float]]] = {}
        for fp in timetable.footpaths:
            adj.setdefault(fp.from_stop, []).append(
                (fp.to_stop, fp.duration.total_seconds()))
        cap = _MAX_TRANSFER_CHAIN.total_seconds()
        out: dict[str, list[Footpath]] = {}
        for src in adj:
            best: dict[str, float] = {src: 0.0}
            pq: list[tuple[float, str]] = [(0.0, src)]
            while pq:
                d, u = heapq.heappop(pq)
                if d > best[u]:
                    continue                     # stale heap entry
                for v, w in adj.get(u, ()):
                    nd = d + w
                    if nd <= cap and nd < best.get(v, cap + 1.0):
                        best[v] = nd
                        heapq.heappush(pq, (nd, v))
            # A direct footpath the feed supplied is always honoured, even past the
            # cap (the cap only limits how far walks are *chained*); take the better
            # of the direct edge and any shorter composed path found above.
            for v, w in adj[src]:
                if w < best.get(v, w + 1.0):
                    best[v] = w
            fps = [Footpath(src, v, timedelta(seconds=sec))
                   for v, sec in best.items() if v != src]
            if fps:
                out[src] = fps
        return out

    @staticmethod
    def _earlier(a, b):
        if a is None:
            return b
        if b is None:
            return a
        return a if a <= b else b

    def _relax_footpaths(self, stop: str, arr_foot: dict, arr_veh: dict,
                         pred_foot: dict) -> None:
        # Walk from the earliest physical arrival at `stop` (whether reached on
        # foot or by vehicle); the destination is reached ON FOOT -- ready to board
        # immediately, no change time (the footpath duration is the change cost).
        base = self._earlier(arr_foot.get(stop), arr_veh.get(stop))
        if base is None:
            return
        for fp in self._fp_from.get(stop, ()):
            cand = base + fp.duration
            if arr_foot.get(fp.to_stop) is None or cand < arr_foot[fp.to_stop]:
                arr_foot[fp.to_stop] = cand
                pred_foot[fp.to_stop] = ("foot", fp)

    def _scan(self, sources: dict[str, datetime], target: str | None,
              conditions: frozenset[str],
              allowed_modes: frozenset | None = None):
        # Two labels per stop: arr_foot is the earliest "ready to board" arrival
        # (a source or a footpath -- no change time needed), arr_veh the earliest
        # arrival ENDING ON A VEHICLE (needs the stop's change time to board a
        # different run). They are tracked independently because a later walk
        # arrival can be ready for a tight onward departure that an earlier vehicle
        # arrival, needing the change time, would miss.
        if not sources:
            return {}, {}, {}, {}, {}
        # Normalize source times to the timetable's frame: a naive time is read as
        # local at its own stop and converted to UTC (a tz-aware feed), or left
        # naive (a feed with no timezone data). After this every datetime in the
        # scan shares one frame, so all comparisons are well-defined.
        sources = {s: self.tt.localize(s, t) for s, t in sources.items()}
        t0 = min(sources.values())
        # Horizon is measured from each source's ready time, so the window end
        # tracks the latest source, not the earliest.
        t_end = max(sources.values()) + self.horizon
        cache_key = (t0, t_end, conditions)
        conns = self._conn_cache.get(cache_key)
        if conns is None:
            conns = self.tt.connections(t0, t_end, conditions)
            self._conn_cache[cache_key] = conns

        arr_foot: dict[str, datetime] = {}
        arr_veh: dict[str, datetime] = {}
        pred_foot: dict[str, tuple] = {}      # ("source",) | ("foot", fp)
        pred_veh: dict[str, tuple] = {}       # ("trip", board_c, last_c)
        boarded: dict[str, Connection] = {}   # run_id -> boarding connection
        board_basis: dict[str, str] = {}      # run_id -> "foot"|"veh" at boarding

        for stop, t in sources.items():
            if arr_foot.get(stop) is None or t < arr_foot[stop]:
                arr_foot[stop] = t
                pred_foot[stop] = ("source",)
        for stop in list(arr_foot.keys()):
            self._relax_footpaths(stop, arr_foot, arr_veh, pred_foot)

        for c in conns:
            if target is not None:
                bt = self._earlier(arr_foot.get(target), arr_veh.get(target))
                if bt is not None and c.departure > bt:
                    break  # sorted by departure: nothing later can improve target
            if allowed_modes is not None and c.mode not in allowed_modes:
                continue
            u, v, r = c.dep_stop, c.arr_stop, c.run_id
            if r not in boarded:
                af_u, av_u = arr_foot.get(u), arr_veh.get(u)
                # transfer_time is None when changing vehicles is not allowed here
                # (GTFS transfer_type 3): a vehicle arrival can never board another
                # run at u, though a foot/source arrival (af_u) still can.
                change = self.tt.transfer_time(u)
                veh_ready = (av_u + change
                             if av_u is not None and change is not None else None)
                ready = self._earlier(af_u, veh_ready)
                if ready is None or ready > c.departure:
                    continue
                boarded[r] = c
                # Record which label made us ready, so reconstruction chains the
                # feasible predecessor at the boarding stop (foot wins ties: no
                # change time).
                board_basis[r] = ("foot" if af_u is not None
                                  and (veh_ready is None or af_u <= veh_ready)
                                  else "veh")
            if arr_veh.get(v) is None or c.arrival < arr_veh[v]:
                prev_best = self._earlier(arr_foot.get(v), arr_veh.get(v))
                arr_veh[v] = c.arrival
                pred_veh[v] = ("trip", boarded[r], c)
                if prev_best is None or c.arrival < prev_best:
                    self._relax_footpaths(v, arr_foot, arr_veh, pred_foot)

        return arr_foot, arr_veh, pred_foot, pred_veh, board_basis

    def arrival_times(self, sources: dict[str, datetime],
                      conditions: frozenset[str] = frozenset(),
                      allowed_modes: frozenset | None = None
                      ) -> dict[str, datetime]:
        """Earliest arrival time at every reachable stop (for coupling)."""
        af, av, _, _, _ = self._scan(sources, None, conditions, allowed_modes)
        return {s: self._earlier(af.get(s), av.get(s)) for s in set(af) | set(av)}

    def query(self, sources: dict[str, datetime], target: str,
              conditions: frozenset[str] = frozenset(),
              allowed_modes: frozenset | None = None) -> Journey | None:
        """Earliest-arrival journey from any source to target, or None.

        allowed_modes (if given) restricts which vehicle modes may be used in
        the line-haul (footpath transfers are always allowed).
        """
        if not sources:
            return None
        af, av, pf, pv, bb = self._scan(sources, target, conditions, allowed_modes)
        af_t, av_t = af.get(target), av.get(target)
        if self._earlier(af_t, av_t) is None:
            return None
        label = ("foot" if af_t is not None and (av_t is None or af_t <= av_t)
                 else "veh")
        if label == "foot" and pf.get(target) == ("source",):
            return None  # target is itself a source: no actual journey
        return self._reconstruct(target, label, af, av, pf, pv, bb)

    def _one_seat_scan(self, sources: dict[str, datetime],
                       conditions: frozenset[str],
                       allowed_modes: frozenset | None = None):
        # Best single-vehicle ("one-seat") arrival at every stop: walk from a source
        # to a boarding stop (0 trips), ride exactly one run, then optionally walk to
        # the stop. The earliest-arrival scan rides whatever gets there soonest, which
        # on a corridor with a fast change beats a slower through-service -- so a
        # direct train is never produced. This one finds it, for MOST_DIRECT to rank.
        if not sources:
            return {}, {}, {}, {}
        sources = {s: self.tt.localize(s, t) for s, t in sources.items()}
        t0 = min(sources.values())
        t_end = max(sources.values()) + self.horizon
        cache_key = (t0, t_end, conditions)
        conns = self._conn_cache.get(cache_key)
        if conns is None:
            conns = self.tt.connections(t0, t_end, conditions)
            self._conn_cache[cache_key] = conns

        # 0-trip readiness: when you can be at a stop having only walked from a
        # source. A source's own footpaths are transitively closed, so one pass.
        foot_ready: dict[str, datetime] = {}
        pred_ready: dict[str, tuple] = {}     # ("source",) | ("foot", fp)
        for s, t in sources.items():
            if foot_ready.get(s) is None or t < foot_ready[s]:
                foot_ready[s] = t
                pred_ready[s] = ("source",)
        for s in [s for s, p in pred_ready.items() if p == ("source",)]:
            for fp in self._fp_from.get(s, ()):
                cand = foot_ready[s] + fp.duration
                if foot_ready.get(fp.to_stop) is None or cand < foot_ready[fp.to_stop]:
                    foot_ready[fp.to_stop] = cand
                    pred_ready[fp.to_stop] = ("foot", fp)

        boarded: dict[str, Connection] = {}   # run_id -> boarding connection
        arr: dict[str, datetime] = {}         # best one-seat arrival (pre final walk)
        pred_ride: dict[str, tuple] = {}      # stop -> (board_c, alight_c)
        for c in conns:
            if allowed_modes is not None and c.mode not in allowed_modes:
                continue
            u, v, r = c.dep_stop, c.arr_stop, c.run_id
            if r not in boarded:
                ru = foot_ready.get(u)
                if ru is not None and ru <= c.departure:
                    boarded[r] = c        # earliest foot-reachable boarding on the run
            if r in boarded and (arr.get(v) is None or c.arrival < arr[v]):
                arr[v] = c.arrival
                pred_ride[v] = (boarded[r], c)

        # Final on-foot leg from a ride's alighting stop (no extra vehicle).
        final_arr = dict(arr)
        pred_final: dict[str, tuple] = {v: ("ride",) for v in arr}
        for a in list(arr):
            for fp in self._fp_from.get(a, ()):
                cand = arr[a] + fp.duration
                if final_arr.get(fp.to_stop) is None or cand < final_arr[fp.to_stop]:
                    final_arr[fp.to_stop] = cand
                    pred_final[fp.to_stop] = ("foot", fp)
        return final_arr, foot_ready, pred_ride, (pred_final, pred_ready)

    def one_seat_arrivals(self, sources: dict[str, datetime],
                          conditions: frozenset[str] = frozenset(),
                          allowed_modes: frozenset | None = None
                          ) -> dict[str, datetime]:
        """Best single-vehicle arrival at every reachable stop (for egress ranking,
        mirroring arrival_times)."""
        final_arr, _, _, _ = self._one_seat_scan(sources, conditions, allowed_modes)
        return final_arr

    def one_seat_query(self, sources: dict[str, datetime], target: str,
                       conditions: frozenset[str] = frozenset(),
                       allowed_modes: frozenset | None = None) -> Journey | None:
        """The best single-vehicle ("one-seat") journey from any source to target,
        or None: walk to a boarding stop, ride one run, optionally walk to target.
        Same signature as query(), so the coupling can swap one for the other."""
        if not sources:
            return None
        final_arr, foot_ready, pred_ride, (pred_final, pred_ready) = \
            self._one_seat_scan(sources, conditions, allowed_modes)
        if target not in final_arr:
            return None
        legs: list[JourneyLeg] = []
        stop = target
        pf = pred_final.get(stop)
        if pf is not None and pf[0] == "foot":
            fp = pf[1]
            end = final_arr[stop]
            legs.append(JourneyLeg(
                mode=Mode.WALK, from_stop=fp.from_stop, to_stop=fp.to_stop,
                departure=end - fp.duration, arrival=end,
                cost_level=CostLevel.LOW, trip_id=None))
            stop = fp.from_stop                       # the ride's alighting stop
        ride = pred_ride.get(stop)
        if ride is None:
            return None
        board_c, last_c = ride
        legs.append(JourneyLeg(
            mode=board_c.mode, from_stop=board_c.dep_stop, to_stop=last_c.arr_stop,
            departure=board_c.departure, arrival=last_c.arrival,
            cost_level=board_c.cost_level, trip_id=board_c.trip_id))
        pr = pred_ready.get(board_c.dep_stop)
        if pr is not None and pr[0] == "foot":
            fp = pr[1]
            end = foot_ready[board_c.dep_stop]
            legs.append(JourneyLeg(
                mode=Mode.WALK, from_stop=fp.from_stop, to_stop=fp.to_stop,
                departure=end - fp.duration, arrival=end,
                cost_level=CostLevel.LOW, trip_id=None))
        legs.reverse()
        return Journey(legs=tuple(legs))

    def _reconstruct(self, target: str, label: str, arr_foot: dict, arr_veh: dict,
                     pred_foot: dict, pred_veh: dict, board_basis: dict) -> Journey:
        # Each "trip" predecessor already spans a whole boarded run (boarding
        # connection -> alighting connection), so a run is one leg and the back-
        # pointer jumps to the boarding stop, skipping interior stops. `label`
        # tracks whether the current stop is explained by its foot or vehicle label.
        steps: list[tuple] = []
        stop, lab = target, label
        while True:
            if lab == "veh":
                _, board_c, last_c = pred_veh[stop]
                steps.append(("trip", board_c, last_c))
                stop = board_c.dep_stop
                lab = board_basis[board_c.run_id]      # feasible basis at boarding
            else:
                entry = pred_foot[stop]
                if entry[0] == "source":
                    break
                fp = entry[1]
                steps.append(("foot", fp))
                stop = fp.from_stop
                af_s, av_s = arr_foot.get(stop), arr_veh.get(stop)
                lab = ("foot" if af_s is not None and (av_s is None or af_s <= av_s)
                       else "veh")                       # best arrival at walk origin
        steps.reverse()

        legs: list[JourneyLeg] = []
        for step in steps:
            if step[0] == "foot":
                fp = step[1]
                end = arr_foot[fp.to_stop]
                legs.append(JourneyLeg(
                    mode=Mode.WALK, from_stop=fp.from_stop, to_stop=fp.to_stop,
                    departure=end - fp.duration, arrival=end,
                    cost_level=CostLevel.LOW, trip_id=None))
            else:
                board_c, last_c = step[1], step[2]
                legs.append(JourneyLeg(
                    mode=board_c.mode, from_stop=board_c.dep_stop,
                    to_stop=last_c.arr_stop, departure=board_c.departure,
                    arrival=last_c.arrival, cost_level=board_c.cost_level,
                    trip_id=board_c.trip_id))
        return Journey(legs=tuple(legs))
