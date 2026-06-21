"""Timetable model for the scheduled layer (trains, ferries, flights).

GTFS-aligned but minimal: trips are templates of stop-times (offsets from the
service-day midnight) gated by a Validity (calendar / season / conditions).
Concrete Connections (absolute datetimes) are materialized per query for the
relevant service dates, which is what the Connection Scan Algorithm consumes.

Times are treated as naive local times. Multi-timezone flight handling is a
known limitation to be addressed when international air schedules are added.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from travelplanner.models import CostLevel, Mode
from travelplanner.graph.schema import NodeType
from travelplanner.graph.validity import ALWAYS, Validity

# Minimum time to change vehicles at a stop, used both as the per-Stop default
# and as the fallback for a stop a trip passes through but that was never
# registered (so an unregistered interior stop never gets a 0-second transfer).
DEFAULT_MIN_TRANSFER = timedelta(minutes=5)


def parse_gtfs_time(value: str) -> timedelta:
    """Parse 'HH:MM' or 'HH:MM:SS' (hours may exceed 24 for overnight trips)."""
    parts = value.strip().split(":")
    if len(parts) == 2:
        h, m, s = int(parts[0]), int(parts[1]), 0
    elif len(parts) == 3:
        h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
    else:
        raise ValueError(f"bad time {value!r}")
    return timedelta(hours=h, minutes=m, seconds=s)


@dataclass(frozen=True)
class Stop:
    id: str
    name: str
    lat: float
    lon: float
    type: NodeType = NodeType.RAIL_STATION
    min_transfer: timedelta = DEFAULT_MIN_TRANSFER


@dataclass(frozen=True)
class StopTime:
    stop_id: str
    arrival: timedelta    # offset from service-day midnight
    departure: timedelta


@dataclass(frozen=True)
class Trip:
    id: str
    mode: Mode
    stop_times: tuple[StopTime, ...]
    validity: Validity = ALWAYS
    cost_level: CostLevel = CostLevel.MEDIUM

    def __post_init__(self) -> None:
        # Stop times must be non-decreasing (arr0 <= dep0 <= arr1 <= dep1 ...).
        # A later stop departing before an earlier one breaks the scan's
        # departure-order precondition and silently strands the trip's tail.
        times = [t for st in self.stop_times for t in (st.arrival, st.departure)]
        if any(b < a for a, b in zip(times, times[1:])):
            raise ValueError(
                f"trip {self.id!r} stop_times are not in non-decreasing time order")


@dataclass(frozen=True)
class Footpath:
    from_stop: str
    to_stop: str
    duration: timedelta

    def __post_init__(self) -> None:
        # A non-positive footpath would let a walk reach a stop at or before its
        # own start (time travel), corrupting earliest-arrival times.
        if self.duration <= timedelta():
            raise ValueError(
                f"footpath {self.from_stop!r}->{self.to_stop!r} must have a "
                f"positive duration, got {self.duration}")


@dataclass(frozen=True)
class Connection:
    dep_stop: str
    arr_stop: str
    departure: datetime
    arrival: datetime
    trip_id: str       # base trip id (for display)
    run_id: str        # trip id + service date (one concrete vehicle run)
    mode: Mode
    cost_level: CostLevel


def make_trip(trip_id: str, mode: Mode, schedule, *,
              validity: Validity = ALWAYS,
              cost_level: CostLevel = CostLevel.MEDIUM) -> Trip:
    """Build a Trip from (stop_id, arrival, departure) rows.

    arrival/departure may be 'HH:MM[:SS]' strings or timedeltas.
    """
    def as_td(x) -> timedelta:
        return x if isinstance(x, timedelta) else parse_gtfs_time(x)

    stop_times = tuple(
        StopTime(stop_id=sid, arrival=as_td(arr), departure=as_td(dep))
        for sid, arr, dep in schedule
    )
    return Trip(id=trip_id, mode=mode, stop_times=stop_times,
                validity=validity, cost_level=cost_level)


@dataclass
class Timetable:
    stops: dict[str, Stop] = field(default_factory=dict)
    trips: dict[str, Trip] = field(default_factory=dict)
    footpaths: list[Footpath] = field(default_factory=list)

    def add_stop(self, stop: Stop) -> Stop:
        self.stops[stop.id] = stop
        return stop

    def add_trip(self, trip: Trip) -> Trip:
        self.trips[trip.id] = trip
        return trip

    def add_footpath(self, from_stop: str, to_stop: str,
                     duration: timedelta) -> None:
        self.footpaths.append(Footpath(from_stop, to_stop, duration))

    def footpaths_from(self) -> dict[str, list[Footpath]]:
        out: dict[str, list[Footpath]] = {}
        for fp in self.footpaths:
            out.setdefault(fp.from_stop, []).append(fp)
        return out

    def transfer_time(self, stop_id: str) -> timedelta:
        stop = self.stops.get(stop_id)
        return stop.min_transfer if stop else DEFAULT_MIN_TRANSFER

    def connections(self, start: datetime, end: datetime,
                    conditions: frozenset[str] = frozenset()) -> list[Connection]:
        """Materialize connections for a [start, end] BOARDING window, sorted by
        departure. The window bounds where a run may be BOARDED; once a run has a
        boardable segment it is materialized in full (later segments departing
        after `end` are kept so the scan can ride through). The service-date
        look-back covers the largest stop-time offset, so multi-night runs whose
        service date is several days earlier are still captured.
        """
        out: list[Connection] = []
        max_offset = max((st.departure for trip in self.trips.values()
                          for st in trip.stop_times), default=timedelta())
        day = (start - max(timedelta(days=1), max_offset)).date()
        last = end.date()
        while day <= last:
            midnight = datetime(day.year, day.month, day.day)
            iso = day.isoformat()
            for trip in self.trips.values():
                if not trip.validity.is_active(day, conditions):
                    continue
                sts = trip.stop_times
                run_id = f"{trip.id}@{iso}"
                segs: list[Connection] = []
                boardable = False
                for a, b in zip(sts, sts[1:]):
                    dep = midnight + a.departure
                    arr = midnight + b.arrival
                    if dep < start:
                        continue       # cannot board (nor ride-through) before t0
                    # A vehicle segment between two distinct stops must advance
                    # time. Non-positive-duration segments are invalid data and
                    # would also break the scan's departure-order precondition
                    # (co-located instantaneous links belong in footpaths).
                    if arr <= dep:
                        continue
                    segs.append(Connection(
                        dep_stop=a.stop_id, arr_stop=b.stop_id,
                        departure=dep, arrival=arr,
                        trip_id=trip.id, run_id=run_id,
                        mode=trip.mode, cost_level=trip.cost_level,
                    ))
                    if dep <= end:
                        boardable = True   # at least one segment can be boarded
                if boardable:
                    out.extend(segs)       # keep the run's tail past `end` too
            day += timedelta(days=1)
        out.sort(key=lambda c: (c.departure, c.arrival))
        return out
