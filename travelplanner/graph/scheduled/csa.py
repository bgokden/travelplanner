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

    def _relax_footpaths(self, stop: str, arr: dict, by_trip: dict,
                         pred: dict) -> None:
        for fp in self._fp_from.get(stop, ()):
            cand = arr[stop] + fp.duration
            if fp.to_stop not in arr or cand < arr[fp.to_stop]:
                arr[fp.to_stop] = cand
                by_trip[fp.to_stop] = False
                pred[fp.to_stop] = ("foot", fp)

    def _scan(self, sources: dict[str, datetime], target: str | None,
              conditions: frozenset[str],
              allowed_modes: frozenset | None = None):
        t0 = min(sources.values())
        # Horizon is measured from each source's ready time, so the window end
        # tracks the latest source, not the earliest.
        t_end = max(sources.values()) + self.horizon
        conns = self.tt.connections(t0, t_end, conditions)

        arr: dict[str, datetime] = {}
        by_trip: dict[str, bool] = {}
        pred: dict[str, tuple] = {}
        reachable: set[str] = set()

        for stop, t in sources.items():
            if stop not in arr or t < arr[stop]:
                arr[stop] = t
                by_trip[stop] = False
                pred[stop] = ("source",)
        for stop in list(arr.keys()):
            self._relax_footpaths(stop, arr, by_trip, pred)

        for c in conns:
            if target is not None and target in arr and c.departure > arr[target]:
                break  # sorted by departure: nothing later can improve target
            if allowed_modes is not None and c.mode not in allowed_modes:
                continue
            if c.run_id not in reachable:
                ready = arr.get(c.dep_stop)
                if ready is None:
                    continue
                if by_trip.get(c.dep_stop):
                    ready = ready + self.tt.transfer_time(c.dep_stop)
                if ready > c.departure:
                    continue
                reachable.add(c.run_id)
            if c.arr_stop not in arr or c.arrival < arr[c.arr_stop]:
                arr[c.arr_stop] = c.arrival
                by_trip[c.arr_stop] = True
                pred[c.arr_stop] = ("trip", c)
                self._relax_footpaths(c.arr_stop, arr, by_trip, pred)

        return arr, pred

    def arrival_times(self, sources: dict[str, datetime],
                      conditions: frozenset[str] = frozenset(),
                      allowed_modes: frozenset | None = None
                      ) -> dict[str, datetime]:
        """Earliest arrival time at every reachable stop (for coupling)."""
        arr, _ = self._scan(sources, None, conditions, allowed_modes)
        return arr

    def query(self, sources: dict[str, datetime], target: str,
              conditions: frozenset[str] = frozenset(),
              allowed_modes: frozenset | None = None) -> Journey | None:
        """Earliest-arrival journey from any source to target, or None.

        allowed_modes (if given) restricts which vehicle modes may be used in
        the line-haul (footpath transfers are always allowed).
        """
        if not sources:
            return None
        arr, pred = self._scan(sources, target, conditions, allowed_modes)
        if target not in arr or pred.get(target) == ("source",):
            return None
        return self._reconstruct(target, pred, arr)

    def _reconstruct(self, target: str, pred: dict, arr: dict) -> Journey:
        steps: list[tuple] = []
        stop = target
        while pred[stop][0] != "source":
            kind = pred[stop][0]
            if kind == "trip":
                c: Connection = pred[stop][1]
                steps.append(("trip", c))
                stop = c.dep_stop
            else:  # foot
                fp = pred[stop][1]
                steps.append(("foot", fp))
                stop = fp.from_stop
        steps.reverse()

        legs: list[JourneyLeg] = []
        i = 0
        while i < len(steps):
            kind, payload = steps[i]
            if kind == "foot":
                fp = payload
                end = arr[fp.to_stop]
                legs.append(JourneyLeg(
                    mode=Mode.WALK, from_stop=fp.from_stop, to_stop=fp.to_stop,
                    departure=end - fp.duration, arrival=end,
                    cost_level=CostLevel.LOW, trip_id=None))
                i += 1
                continue
            # merge consecutive connections on the same run into one leg
            first: Connection = payload
            last: Connection = payload
            j = i + 1
            while j < len(steps) and steps[j][0] == "trip" \
                    and steps[j][1].run_id == first.run_id:
                last = steps[j][1]
                j += 1
            legs.append(JourneyLeg(
                mode=first.mode, from_stop=first.dep_stop,
                to_stop=last.arr_stop, departure=first.departure,
                arrival=last.arrival, cost_level=first.cost_level,
                trip_id=first.trip_id))
            i = j

        return Journey(legs=tuple(legs))
