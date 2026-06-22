"""Connection Scan Algorithm: earliest-arrival routing on a Timetable.

A single forward scan over departure-sorted connections. Trip-based: once a
vehicle run is boarded it can be ridden without a transfer penalty; boarding a
*different* run after alighting requires the stop's minimum change time.
Multi-source by construction (seed any number of (stop, ready-time) pairs),
which is what the road/transit coupling in Phase 3 needs.

Footpaths are assumed transitively closed (a single relaxation step is applied
per stop improvement).
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from travelplanner.models import CostLevel, Mode
from travelplanner.graph.scheduled.model import Connection, Footpath, Timetable


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
        # improvement is sufficient (the scan never re-relaxes a stop reached
        # only by walking). The footpath graph is small.
        self._fp_from = self._closed_footpaths(timetable)

    @staticmethod
    def _closed_footpaths(timetable: Timetable) -> dict[str, list[Footpath]]:
        dist: dict[tuple[str, str], float] = {}
        nodes: set[str] = set()
        for fp in timetable.footpaths:
            nodes.add(fp.from_stop)
            nodes.add(fp.to_stop)
            key = (fp.from_stop, fp.to_stop)
            sec = fp.duration.total_seconds()
            if key not in dist or sec < dist[key]:
                dist[key] = sec
        for k in nodes:
            for i in nodes:
                ik = dist.get((i, k))
                if ik is None:
                    continue
                for j in nodes:
                    if i == j:
                        continue
                    kj = dist.get((k, j))
                    if kj is None:
                        continue
                    cand = ik + kj
                    key = (i, j)
                    if key not in dist or cand < dist[key]:
                        dist[key] = cand
        out: dict[str, list[Footpath]] = {}
        for (i, j), sec in dist.items():
            if i == j:
                continue
            out.setdefault(i, []).append(
                Footpath(i, j, timedelta(seconds=sec)))
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
        conns = self.tt.connections(t0, t_end, conditions)

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
                veh_ready = (av_u + self.tt.transfer_time(u)
                             if av_u is not None else None)
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
