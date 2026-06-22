"""Timetable model for the scheduled layer (trains, ferries, flights).

GTFS-aligned but minimal: trips are templates of stop-times (offsets from the
service-day midnight) gated by a Validity (calendar / season / conditions).
Concrete Connections (absolute datetimes) are materialized per query for the
relevant service dates, which is what the Connection Scan Algorithm consumes.

Times are treated as naive local times. Multi-timezone flight handling is a
known limitation to be addressed when international air schedules are added.
"""

from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from travelplanner.models import CostLevel, Mode
from travelplanner.graph.schema import NodeType
from travelplanner.graph.validity import ALWAYS, Validity

# Minimum time to change vehicles at a stop, used both as the per-Stop default
# and as the fallback for a stop a trip passes through but that was never
# registered (so an unregistered interior stop never gets a 0-second transfer).
DEFAULT_MIN_TRANSFER = timedelta(minutes=5)

_UTC = timezone.utc


@lru_cache(maxsize=512)
def _zone(name: str) -> ZoneInfo:
    return ZoneInfo(name)


def valid_tz(name: str | None) -> str | None:
    """Return `name` if it is a loadable IANA timezone, else None.

    Feeds carry junk in their timezone fields -- OpenFlights writes '\\N' for an
    unknown zone, and an agency_timezone can be blank or typo'd. Cleaning these at
    the loader boundary lets the scan trust every Stop.tz (an unloadable zone
    would otherwise crash connection materialization mid-query).
    """
    if not name:
        return None
    try:
        _zone(name)
        return name
    except (ZoneInfoNotFoundError, ValueError):
        return None


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
    # IANA timezone name (e.g. "Europe/Amsterdam") for this stop's local times.
    # None means "unknown"; the scan treats an unknown-tz stop as a single
    # default zone, so a single-timezone feed behaves exactly as before. It is
    # only consulted once connections are materialized in absolute (UTC) time.
    tz: str | None = None


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

    def tz_aware(self) -> bool:
        """True once any stop carries an IANA timezone. A feed with no timezone
        data stays naive (we never invent a zone we do not have); only a tz-aware
        feed materializes connections in absolute UTC time."""
        return any(s.tz for s in self.stops.values())

    def _default_tz(self) -> str | None:
        """The most common stop timezone, used for a tz-aware feed's stops that
        lack their own tz (so an aware feed never mixes naive and aware times)."""
        names = Counter(s.tz for s in self.stops.values() if s.tz)
        return names.most_common(1)[0][0] if names else None

    def localize(self, stop_id: str, when: datetime) -> datetime:
        """Normalize a source time for the scan.

        A naive time is read as local at `stop_id` (the "local to the start" rule)
        and converted to absolute UTC; an already-aware time is converted to UTC
        as-is. For a feed with no timezone data the time passes through unchanged,
        so the scan stays naive.
        """
        if not self.tz_aware():
            # Naive feed: the scan compares against naive materialized times, so
            # an aware source would raise on comparison -- drop the tzinfo, keeping
            # its wall clock as the feed's local time.
            return when.replace(tzinfo=None) if when.tzinfo is not None else when
        if when.tzinfo is not None:
            return when.astimezone(_UTC)
        stop = self.stops.get(stop_id)
        name = stop.tz if stop and stop.tz else self._default_tz()
        zone = _zone(name) if name else _UTC
        return when.replace(tzinfo=zone).astimezone(_UTC)

    def zone_for_point(self, lat: float, lon: float) -> str | None:
        """IANA timezone of the stop nearest (lat, lon), the feed default if that
        stop has none, or None for a feed with no timezone data. Used to read a
        naive departure time as local at the trip's origin (a door coordinate has
        no stop of its own)."""
        if not self.tz_aware():
            return None
        from travelplanner.geo import haversine
        nearest = min(self.stops.values(), default=None,
                      key=lambda s: haversine(lat, lon, s.lat, s.lon))
        if nearest is None:
            return None
        return nearest.tz or self._default_tz()

    def _trip_zone(self, first_stop_id: str, default_tz: str | None):
        """The single zone a trip's stop-times are expressed in: its first stop's
        zone. GTFS stop_times are ALL in the agency timezone (carried by every
        stop, so the first stop has it); OpenFlights expresses a flight's arrival
        offset relative to the departure airport, so the departure (first) zone is
        correct for both. Using one zone per trip -- rather than each stop's own --
        keeps a cross-timezone leg monotonic in UTC instead of appearing to arrive
        before it departed."""
        stop = self.stops.get(first_stop_id)
        name = (stop.tz if stop and stop.tz else default_tz)
        return _zone(name) if name else _UTC

    def _materialize(self, day_midnight: datetime, offset: timedelta,
                     zone) -> datetime:
        """Absolute UTC datetime for a stop-time offset on a service day, taken in
        the trip's `zone`. The offset is wall-clock from local service-day
        midnight (GTFS hours may exceed 24 for overnight runs); zoneinfo applies
        the correct DST offset at that wall time."""
        return (day_midnight.replace(tzinfo=zone) + offset).astimezone(_UTC)

    def connections(self, start: datetime, end: datetime,
                    conditions: frozenset[str] = frozenset()) -> list[Connection]:
        """Materialize connections for a [start, end] BOARDING window, sorted by
        departure. The window bounds where a run may be BOARDED; once a run has a
        boardable segment it is materialized in full (later segments departing
        after `end` are kept so the scan can ride through). The service-date
        look-back covers the largest stop-time offset, so multi-night runs whose
        service date is several days earlier are still captured.

        Times are naive local for a feed with no timezone data; for a tz-aware
        feed every Connection time is absolute UTC (each stop localized in its own
        zone). `start`/`end` must match that convention -- aware UTC for a tz-aware
        feed -- which the scan guarantees by deriving them from its sources.
        """
        out: list[Connection] = []
        aware = self.tz_aware()
        default_tz = self._default_tz() if aware else None
        max_offset = max((st.departure for trip in self.trips.values()
                          for st in trip.stop_times), default=timedelta())
        day = (start - max(timedelta(days=1), max_offset)).date()
        # A local service date materializes to UTC up to a full day off (max tz
        # offset < 24h), so an aware feed scans one extra day forward; the
        # dep<=end / boardable filters drop anything genuinely out of window.
        last = (end + timedelta(days=1)).date() if aware else end.date()
        while day <= last:
            midnight = datetime(day.year, day.month, day.day)
            iso = day.isoformat()
            for trip in self.trips.values():
                if not trip.validity.is_active(day, conditions):
                    continue
                sts = trip.stop_times
                run_id = f"{trip.id}@{iso}"
                trip_zone = (self._trip_zone(sts[0].stop_id, default_tz)
                             if aware else None)
                segs: list[Connection] = []
                boardable = False
                for a, b in zip(sts, sts[1:]):
                    if aware:
                        dep = self._materialize(midnight, a.departure, trip_zone)
                        arr = self._materialize(midnight, b.arrival, trip_zone)
                    else:
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


def merge_timetables(*timetables: Timetable) -> Timetable:
    """Combine several timetables into one (e.g. a flight network plus a GTFS
    feed) for a single scan. Stops and trips are first-wins on id collision and
    footpaths are concatenated, so pass the more specific feed first. Feed ids are
    globally unique in practice (GTFS ids, IATA-pair flight ids), so a collision
    means the same entity and dropping the duplicate is correct."""
    merged = Timetable()
    for tt in timetables:
        for sid, stop in tt.stops.items():
            merged.stops.setdefault(sid, stop)
        for tid, trip in tt.trips.items():
            merged.trips.setdefault(tid, trip)
        merged.footpaths.extend(tt.footpaths)
    return merged


def fill_missing_tz(tt: Timetable) -> Timetable:
    """Give every tz-less stop the timezone of its nearest tz-bearing stop.

    When a feed with no timezone data (a GTFS feed lacking agency_timezone) is
    merged into a tz-aware timetable (e.g. the flight network), its stops would
    otherwise inherit the table's single most-common zone -- geographically wrong
    if the trip spans zones. Filling from the nearest located stop keeps each one
    in a sensible local zone. Mutates and returns `tt`; a no-op when no stop has a
    timezone or all already do.
    """
    from travelplanner.geo import haversine
    have = [s for s in tt.stops.values() if s.tz]
    missing = [s for s in tt.stops.values() if not s.tz]
    if not have or not missing:
        return tt
    for s in missing:
        nearest = min(have, key=lambda h: haversine(s.lat, s.lon, h.lat, h.lon))
        tt.stops[s.id] = replace(s, tz=nearest.tz)
    return tt


def clip_timetable(tt: Timetable, min_lat: float, min_lon: float,
                   max_lat: float, max_lon: float) -> Timetable:
    """Restrict a timetable to a corridor bounding box: keep a trip only if at
    least two of its stops fall inside the box, but keep that trip WHOLE (all its
    stop-times and the stops they reference, even just-outside ones) so the route
    stays continuous. This drops trips entirely outside the corridor -- the bulk
    of a national feed -- so the scan over an auto-fetched feed stays tractable
    without distorting any kept route.
    """
    in_box = {sid for sid, s in tt.stops.items()
              if min_lat <= s.lat <= max_lat and min_lon <= s.lon <= max_lon}
    out = Timetable()
    for tid, trip in tt.trips.items():
        ids = [st.stop_id for st in trip.stop_times]
        if sum(1 for x in ids if x in in_box) < 2:
            continue
        out.trips[tid] = trip
        for sid in ids:
            if sid in tt.stops and sid not in out.stops:
                out.stops[sid] = tt.stops[sid]
    out.footpaths = [fp for fp in tt.footpaths
                     if fp.from_stop in out.stops and fp.to_stop in out.stops]
    return out
