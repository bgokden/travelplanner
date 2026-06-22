"""Load a Timetable from a GTFS feed directory (pure stdlib csv).

Reads stops, routes, trips, stop_times, and the service calendar
(calendar.txt + calendar_dates.txt). Service calendars become Validity
objects; route_type maps to a travel Mode.

Scope: rail (route_type 2), ferry (4 and extended water 1000-1099, 1200), and
air (extended 1100-1199). Other route types are loaded as generic scheduled
service mapped to TRAIN, which is noted rather than silently dropped.
"""

import csv
import io
import os
from collections import Counter
from datetime import date, timedelta

from travelplanner.models import CostLevel, Mode
from travelplanner.graph.schema import NodeType
from travelplanner.graph.scheduled.model import (
    Stop,
    StopTime,
    Timetable,
    Trip,
    parse_gtfs_time,
    valid_tz,
)
from travelplanner.graph.validity import ServiceCalendar, Validity

_WEEKDAY_COLS = ["monday", "tuesday", "wednesday", "thursday",
                 "friday", "saturday", "sunday"]


def _parse_date(yyyymmdd: str) -> date:
    s = yyyymmdd.strip()
    return date(int(s[0:4]), int(s[4:6]), int(s[6:8]))


def _mode_for(route_type: int) -> Mode:
    # 4 = ferry; 1000-1099 = Water Transport Service (extended); 1200 = ferry.
    if route_type == 4 or 1000 <= route_type <= 1099 or route_type == 1200:
        return Mode.FERRY
    if 1100 <= route_type <= 1199:
        return Mode.FLIGHT
    return Mode.TRAIN


def _cost_for(mode: Mode) -> CostLevel:
    if mode is Mode.FLIGHT:
        return CostLevel.HIGH
    return CostLevel.MEDIUM


def _to_int(value, default=None):
    """Tolerant int: accepts float-exported numerics ('1.0'); default on failure."""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _to_float(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _rows(path: str):
    # Decode the whole file with a Latin-1 fallback (some European agencies ship
    # Windows-1252/Latin-1 feeds); reading fully first keeps the fallback clean
    # rather than re-yielding a partially-consumed stream.
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            text = f.read()
    except UnicodeDecodeError:
        with open(path, newline="", encoding="latin-1") as f:
            text = f.read()
    yield from csv.DictReader(io.StringIO(text))


def _load_services(feed_dir: str) -> tuple[dict[str, Validity], bool]:
    """Return (service_id -> Validity, has_calendar). has_calendar is True when a
    calendar.txt or calendar_dates.txt exists, so the caller can tell 'this feed
    defines services (skip a trip whose service has no active days)' apart from
    'this feed has no calendar at all (default to always-active)'."""
    services: dict[str, Validity] = {}
    cal_path = os.path.join(feed_dir, "calendar.txt")
    dates_path = os.path.join(feed_dir, "calendar_dates.txt")
    has_calendar = os.path.exists(cal_path) or os.path.exists(dates_path)
    if os.path.exists(cal_path):
        for r in _rows(cal_path):
            weekdays = frozenset(
                i for i, col in enumerate(_WEEKDAY_COLS)
                if r.get(col, "0").strip() == "1")
            cal = ServiceCalendar(
                start=_parse_date(r["start_date"]),
                end=_parse_date(r["end_date"]),
                weekdays=weekdays,
            )
            services[r["service_id"]] = Validity(calendar=cal)

    if os.path.exists(dates_path):
        added: dict[str, set] = {}
        removed: dict[str, set] = {}
        for r in _rows(dates_path):
            sid = r["service_id"]
            d = _parse_date(r["date"])
            et = r.get("exception_type", "").strip()
            if et == "1":
                added.setdefault(sid, set()).add(d)
            elif et == "2":
                removed.setdefault(sid, set()).add(d)
            # else: a malformed exception_type is ignored, not treated as a
            # removal (which would silently cancel an intended service day).
        for sid in set(added) | set(removed):
            base = services.get(sid)
            if base is not None and base.calendar is not None:
                cal = base.calendar
                services[sid] = Validity(calendar=ServiceCalendar(
                    start=cal.start, end=cal.end, weekdays=cal.weekdays,
                    added=cal.added | frozenset(added.get(sid, set())),
                    removed=cal.removed | frozenset(removed.get(sid, set())),
                ))
            else:
                # calendar_dates-only service: span the explicit added dates.
                add = added.get(sid, set())
                rem = removed.get(sid, set())
                if not add:
                    continue
                services[sid] = Validity(calendar=ServiceCalendar(
                    start=min(add), end=max(add), weekdays=frozenset(),
                    added=frozenset(add), removed=frozenset(rem)))
    return services, has_calendar


def _feed_timezone(feed_dir: str) -> str | None:
    """The feed's default IANA timezone from agency.txt.

    GTFS requires agency_timezone, and all stops without a stop_timezone inherit
    their agency's zone. A feed may list several agencies; we take the most common
    timezone as the feed default (a per-stop stop_timezone still overrides it).
    Returns None when agency.txt is absent or carries no usable timezone.
    """
    path = os.path.join(feed_dir, "agency.txt")
    if not os.path.exists(path):
        return None
    tallies: Counter = Counter()
    for r in _rows(path):
        tz = valid_tz((r.get("agency_timezone") or "").strip())
        if tz:
            tallies[tz] += 1
    if not tallies:
        return None
    return tallies.most_common(1)[0][0]


def load_timetable(feed_dir: str) -> Timetable:
    tt = Timetable()

    feed_tz = _feed_timezone(feed_dir)
    for r in _rows(os.path.join(feed_dir, "stops.txt")):
        lat = _to_float(r.get("stop_lat") or "0")
        lon = _to_float(r.get("stop_lon") or "0")
        if lat is None or lon is None:
            continue                       # skip a stop with unparseable coordinates
        stop_tz = valid_tz((r.get("stop_timezone") or "").strip()) or feed_tz
        tt.add_stop(Stop(
            id=r["stop_id"],
            name=r.get("stop_name", ""),
            lat=lat, lon=lon,
            type=NodeType.RAIL_STATION,
            tz=stop_tz,
        ))

    route_mode: dict[str, Mode] = {}
    for r in _rows(os.path.join(feed_dir, "routes.txt")):
        route_mode[r["route_id"]] = _mode_for(_to_int(r.get("route_type"), 2))

    services, has_calendar = _load_services(feed_dir)

    trip_meta: dict[str, tuple[Mode, Validity]] = {}
    for r in _rows(os.path.join(feed_dir, "trips.txt")):
        sid = r["service_id"]
        if has_calendar and sid not in services:
            # Dangling service_id in a feed that defines services: the trip has no
            # service days, so it never runs -- skip it rather than make it active
            # every day via an empty Validity. (A feed with no calendar files at
            # all still falls back to always-active below.)
            continue
        mode = route_mode.get(r["route_id"], Mode.TRAIN)
        trip_meta[r["trip_id"]] = (mode, services.get(sid, Validity()))

    stop_times: dict[str, list[tuple[int, StopTime]]] = {}
    for r in _rows(os.path.join(feed_dir, "stop_times.txt")):
        tid = r["trip_id"]
        arr_s = (r.get("arrival_time") or "").strip()
        dep_s = (r.get("departure_time") or "").strip()
        if not arr_s and not dep_s:
            # A non-timepoint stop may have empty times (GTFS allows it); we do
            # not interpolate, so drop the untimed stop and keep the timed ones.
            continue
        arr_s, dep_s = arr_s or dep_s, dep_s or arr_s
        st = StopTime(
            stop_id=r["stop_id"],
            arrival=parse_gtfs_time(arr_s),
            departure=parse_gtfs_time(dep_s),
        )
        seq = _to_int(r.get("stop_sequence"))
        if seq is None:
            continue                       # skip a row with an unparseable sequence
        stop_times.setdefault(tid, []).append((seq, st))

    for tid, seq in stop_times.items():
        meta = trip_meta.get(tid)
        if meta is None:
            # No trip/service metadata: a dangling service_id (skipped above) or
            # orphan stop_times with no trips.txt entry. The trip has no valid
            # service, so do not materialize it as always-active.
            continue
        mode, validity = meta
        ordered = tuple(st for _, st in sorted(seq, key=lambda x: x[0]))
        if len(ordered) < 2:
            continue
        try:
            tt.add_trip(Trip(id=tid, mode=mode, stop_times=ordered,
                             validity=validity, cost_level=_cost_for(mode)))
        except ValueError:
            continue   # skip a trip with non-monotonic stop_times (invalid data)

    _apply_frequencies(tt, feed_dir)
    return tt


# A single frequencies window expanding to more runs than this is implausible
# (sub-30-second all-day headway) and almost certainly bad data, so it is skipped
# rather than allowed to explode memory.
_MAX_FREQ_RUNS = 2000


def _apply_frequencies(tt: Timetable, feed_dir: str) -> None:
    """Expand frequency-based trips (frequencies.txt) into concrete runs.

    A frequencies row means the trip repeats every headway_secs from start_time to
    end_time; each run shifts the trip's stop-time pattern so its first stop
    departs at that slot. The template trip is itself not a real run, so it is
    replaced by its expansion. Feeds without frequencies.txt are unaffected.
    """
    path = os.path.join(feed_dir, "frequencies.txt")
    if not os.path.exists(path):
        return
    windows: dict[str, list[tuple]] = {}
    for r in _rows(path):
        tid = r.get("trip_id")
        headway = _to_int(r.get("headway_secs"))
        if not tid or tid not in tt.trips or not headway or headway <= 0:
            continue
        try:
            start = parse_gtfs_time((r.get("start_time") or "").strip())
            end = parse_gtfs_time((r.get("end_time") or "").strip())
        except ValueError:
            continue
        if end <= start or (end - start) // timedelta(seconds=headway) > _MAX_FREQ_RUNS:
            continue
        windows.setdefault(tid, []).append(
            (start, end, timedelta(seconds=headway)))

    for tid, rows in windows.items():
        template = tt.trips.pop(tid)
        anchor = template.stop_times[0].departure
        for start, end, headway in rows:
            t = start
            while t < end:
                shift = t - anchor
                sts = tuple(StopTime(st.stop_id, st.arrival + shift,
                                     st.departure + shift)
                            for st in template.stop_times)
                run_id = f"{tid}#{int(t.total_seconds())}"
                try:
                    tt.trips[run_id] = Trip(
                        id=run_id, mode=template.mode, stop_times=sts,
                        validity=template.validity, cost_level=template.cost_level)
                except ValueError:
                    pass        # shifted pattern stays monotonic; defensive only
                t += headway
