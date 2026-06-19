"""Load a Timetable from a GTFS feed directory (pure stdlib csv).

Reads stops, routes, trips, stop_times, and the service calendar
(calendar.txt + calendar_dates.txt). Service calendars become Validity
objects; route_type maps to a travel Mode.

Scope: rail (route_type 2), ferry (4), and air (extended 1100-1199). Other
route types are loaded as generic scheduled service mapped to TRAIN, which is
noted rather than silently dropped.
"""

import csv
import os
from datetime import date, timedelta

from travelplanner.models import CostLevel, Mode
from travelplanner.graph.schema import NodeType
from travelplanner.graph.scheduled.model import (
    Stop,
    StopTime,
    Timetable,
    Trip,
    parse_gtfs_time,
)
from travelplanner.graph.validity import ServiceCalendar, Validity

_WEEKDAY_COLS = ["monday", "tuesday", "wednesday", "thursday",
                 "friday", "saturday", "sunday"]


def _parse_date(yyyymmdd: str) -> date:
    s = yyyymmdd.strip()
    return date(int(s[0:4]), int(s[4:6]), int(s[6:8]))


def _mode_for(route_type: int) -> Mode:
    if route_type == 4 or route_type == 1200:
        return Mode.FERRY
    if 1100 <= route_type <= 1199:
        return Mode.FLIGHT
    return Mode.TRAIN


def _cost_for(mode: Mode) -> CostLevel:
    if mode is Mode.FLIGHT:
        return CostLevel.HIGH
    return CostLevel.MEDIUM


def _rows(path: str):
    with open(path, newline="", encoding="utf-8-sig") as f:
        yield from csv.DictReader(f)


def _load_services(feed_dir: str) -> dict[str, Validity]:
    services: dict[str, Validity] = {}
    cal_path = os.path.join(feed_dir, "calendar.txt")
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

    dates_path = os.path.join(feed_dir, "calendar_dates.txt")
    if os.path.exists(dates_path):
        added: dict[str, set] = {}
        removed: dict[str, set] = {}
        for r in _rows(dates_path):
            sid = r["service_id"]
            d = _parse_date(r["date"])
            if r["exception_type"].strip() == "1":
                added.setdefault(sid, set()).add(d)
            else:
                removed.setdefault(sid, set()).add(d)
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
    return services


def load_timetable(feed_dir: str) -> Timetable:
    tt = Timetable()

    for r in _rows(os.path.join(feed_dir, "stops.txt")):
        tt.add_stop(Stop(
            id=r["stop_id"],
            name=r.get("stop_name", ""),
            lat=float(r.get("stop_lat") or 0.0),
            lon=float(r.get("stop_lon") or 0.0),
            type=NodeType.RAIL_STATION,
        ))

    route_mode: dict[str, Mode] = {}
    for r in _rows(os.path.join(feed_dir, "routes.txt")):
        route_mode[r["route_id"]] = _mode_for(int(r.get("route_type") or 2))

    services = _load_services(feed_dir)

    trip_meta: dict[str, tuple[Mode, Validity]] = {}
    for r in _rows(os.path.join(feed_dir, "trips.txt")):
        mode = route_mode.get(r["route_id"], Mode.TRAIN)
        validity = services.get(r["service_id"], Validity())
        trip_meta[r["trip_id"]] = (mode, validity)

    stop_times: dict[str, list[tuple[int, StopTime]]] = {}
    for r in _rows(os.path.join(feed_dir, "stop_times.txt")):
        tid = r["trip_id"]
        st = StopTime(
            stop_id=r["stop_id"],
            arrival=parse_gtfs_time(r["arrival_time"]),
            departure=parse_gtfs_time(r["departure_time"]),
        )
        stop_times.setdefault(tid, []).append((int(r["stop_sequence"]), st))

    for tid, seq in stop_times.items():
        mode, validity = trip_meta.get(tid, (Mode.TRAIN, Validity()))
        ordered = tuple(st for _, st in sorted(seq, key=lambda x: x[0]))
        if len(ordered) < 2:
            continue
        tt.add_trip(Trip(id=tid, mode=mode, stop_times=ordered,
                         validity=validity, cost_level=_cost_for(mode)))

    return tt
