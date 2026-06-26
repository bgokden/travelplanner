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

# Most vehicle legs the fewest-transfers (RAPTOR-style) scan explores. A journey
# needing more than this many vehicles is not a realistic "fewest changes" option,
# and the bound caps the per-round rescan cost.
_MAX_ROUNDS = 6


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
        # Fewest-transfers rounds, memoized by (sources, conditions, modes): the
        # coupling ranks every egress stop over a single scan, then reconstructs.
        self._mt_cache: dict = {}

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

    def _min_transfer_rounds(self, sources: dict[str, datetime],
                             conditions: frozenset[str],
                             allowed_modes: frozenset | None = None,
                             targets: frozenset | None = None) -> list:
        """RAPTOR-style rounds (memoized). rounds[k] is (arr_foot, arr_veh, pred_foot,
        pred_veh, board_basis) for journeys using AT MOST k vehicle trips: round k may
        board a run only from a stop reached in round k-1, which bounds the trip count,
        so the first round to reach a stop is its fewest-changes journey. Each round
        mirrors the earliest-arrival scan's foot/vehicle labels and change-time rule.
        Memoized per (sources, conditions, modes) -- the coupling ranks every egress
        stop and reconstructs the chosen one over a single scan."""
        if not sources:
            return []
        key = (tuple(sorted(sources.items())), conditions, allowed_modes)
        cached = self._mt_cache.get(key)
        if cached is None:
            # `targets` (the egress stops) only bounds how far the scan runs, not the
            # rounds it returns, so it stays out of the cache key: the first caller
            # (min_transfer_arrivals) seeds it, and the per-target queries reuse the
            # cached rounds.
            cached = self._compute_min_transfer_rounds(
                {s: self.tt.localize(s, t) for s, t in sources.items()},
                conditions, allowed_modes, targets)
            self._mt_cache[key] = cached
        return cached

    def _compute_min_transfer_rounds(self, sources, conditions, allowed_modes,
                                     targets=None):
        t0 = min(sources.values())
        t_end = max(sources.values()) + self.horizon
        cache_key = (t0, t_end, conditions)
        conns = self._conn_cache.get(cache_key)
        if conns is None:
            conns = self.tt.connections(t0, t_end, conditions)
            self._conn_cache[cache_key] = conns

        # Round 0: reachable on foot from the sources, zero vehicle trips.
        af0: dict[str, datetime] = {}
        pf0: dict[str, tuple] = {}
        for s, t in sources.items():
            if af0.get(s) is None or t < af0[s]:
                af0[s] = t
                pf0[s] = ("source",)
        for s in [s for s, p in pf0.items() if p == ("source",)]:
            for fp in self._fp_from.get(s, ()):
                cand = af0[s] + fp.duration
                if af0.get(fp.to_stop) is None or cand < af0[fp.to_stop]:
                    af0[fp.to_stop] = cand
                    pf0[fp.to_stop] = ("foot", fp)
        rounds = [(af0, {}, pf0, {}, {})]

        for _ in range(_MAX_ROUNDS):
            paf, pav = rounds[-1][0], rounds[-1][1]
            af, av = dict(paf), dict(pav)   # carry forward: <=k trips includes <=k-1
            pf: dict[str, tuple] = {}
            pv: dict[str, tuple] = {}
            board_basis: dict[str, str] = {}
            boarded: dict[str, Connection] = {}
            improved = False
            for c in conns:
                if allowed_modes is not None and c.mode not in allowed_modes:
                    continue
                u, v, r = c.dep_stop, c.arr_stop, c.run_id
                if r not in boarded:
                    # Board only from a stop reached in the PREVIOUS round, so this
                    # ride is the round's one extra trip. A foot/source arrival is
                    # ready at once; a vehicle arrival needs the stop's change time
                    # (None there means changing vehicles is not allowed).
                    af_u, av_u = paf.get(u), pav.get(u)
                    change = self.tt.transfer_time(u)
                    veh_ready = (av_u + change
                                 if av_u is not None and change is not None else None)
                    ready = self._earlier(af_u, veh_ready)
                    if ready is not None and ready <= c.departure:
                        boarded[r] = c
                        board_basis[r] = ("foot" if af_u is not None
                                          and (veh_ready is None or af_u <= veh_ready)
                                          else "veh")
                if r in boarded and (av.get(v) is None or c.arrival < av[v]):
                    av[v] = c.arrival
                    pv[v] = ("trip", boarded[r], c)
                    improved = True
                    base = self._earlier(af.get(v), av.get(v))
                    for fp in self._fp_from.get(v, ()):
                        cand = base + fp.duration
                        if af.get(fp.to_stop) is None or cand < af[fp.to_stop]:
                            af[fp.to_stop] = cand
                            pf[fp.to_stop] = ("foot", fp)
            if not improved:
                break                      # a further trip reaches nothing new: done
            rounds.append((af, av, pf, pv, board_basis))
            # Stop once every egress stop is reached: its first-reaching round is its
            # fewest-transfers journey, so later rounds (more changes) are never the
            # ranked choice. The planner only needs the egress, not the whole network,
            # so this avoids exploring all of Germany on a Berlin trip.
            if targets and all((t in af or t in av) for t in targets):
                break
        return rounds

    def min_transfer_arrivals(self, sources: dict[str, datetime],
                              conditions: frozenset[str] = frozenset(),
                              allowed_modes: frozenset | None = None,
                              targets: frozenset | None = None) -> dict:
        """Per stop, (trips, arrival) at its fewest-transfers round -- to rank egress
        stops by changes first, then arrival. A stop reachable on foot alone (round 0)
        is omitted: it carries no vehicle journey for the coupling to use. `targets`
        (the egress stops) bounds the scan: it stops once they are all reached."""
        rounds = self._min_transfer_rounds(sources, conditions, allowed_modes, targets)
        first: dict[str, tuple] = {}
        for k in range(len(rounds)):
            af, av = rounds[k][0], rounds[k][1]
            for stop in set(af) | set(av):
                if stop not in first:
                    a = self._earlier(af.get(stop), av.get(stop))
                    if a is not None:
                        first[stop] = (k, a)
        return {s: ka for s, ka in first.items() if ka[0] >= 1}

    def min_transfer_query(self, sources: dict[str, datetime], target: str,
                           conditions: frozenset[str] = frozenset(),
                           allowed_modes: frozenset | None = None) -> Journey | None:
        """The fewest-transfers journey from any source to target (earliest arrival
        within that change count), or None. Same signature as query(), so the coupling
        can swap one for the other."""
        if not sources:
            return None
        rounds = self._min_transfer_rounds(sources, conditions, allowed_modes)
        for k in range(len(rounds)):
            if self._earlier(rounds[k][0].get(target),
                             rounds[k][1].get(target)) is not None:
                return self._reconstruct_mt(rounds, k, target)
        return None

    def _reconstruct_mt(self, rounds: list, k: int, target: str) -> Journey | None:
        af_t, av_t = rounds[k][0].get(target), rounds[k][1].get(target)
        if self._earlier(af_t, av_t) is None:
            return None
        lab = ("foot" if af_t is not None and (av_t is None or af_t <= av_t)
               else "veh")
        steps: list[tuple] = []
        stop = target
        guard = 0
        while k >= 0:
            guard += 1
            if guard > 100000:
                return None                 # safety against a malformed back-pointer
            af, av, pf, pv, bb = rounds[k]
            if lab == "veh":
                entry = pv.get(stop)
                if entry is not None:
                    _, board_c, last_c = entry
                    steps.append(("trip", board_c, last_c))
                    stop = board_c.dep_stop
                    lab = bb[board_c.run_id]      # how the boarding stop was reached
                k -= 1                            # this trip spent round k; board at k-1
            else:
                entry = pf.get(stop)
                if entry == ("source",):
                    break
                if entry is not None and entry[0] == "foot":
                    fp = entry[1]
                    steps.append(("foot", fp, af[stop]))
                    stop = fp.from_stop
                    af_s, av_s = af.get(stop), av.get(stop)
                    lab = ("foot" if af_s is not None
                           and (av_s is None or af_s <= av_s) else "veh")
                else:
                    k -= 1                        # foot label carried from a prior round
        steps.reverse()
        legs: list[JourneyLeg] = []
        for step in steps:
            if step[0] == "foot":
                fp, end = step[1], step[2]
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
