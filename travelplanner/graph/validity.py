"""Edge/connection validity: when is an edge usable.

Models seasonality and conditions the way production transit systems do:
a GTFS-style service calendar (weekday mask + date range + per-date
exceptions) combined with required/forbidden condition flags (weather,
road status, etc.). The router filters edges by validity for the query's
date and conditions before traversing.
"""

from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True)
class ServiceCalendar:
    """When a service operates, mirroring GTFS calendar.txt + calendar_dates.txt.

    weekdays uses Python's convention: Monday=0 .. Sunday=6.
    `added` forces a date on (a holiday special); `removed` forces it off
    (a cancelled day), each overriding the weekday/range rule.
    """

    start: date
    end: date
    weekdays: frozenset[int] = field(
        default_factory=lambda: frozenset(range(7))
    )
    added: frozenset[date] = field(default_factory=frozenset)
    removed: frozenset[date] = field(default_factory=frozenset)

    def runs_on(self, day: date) -> bool:
        if day in self.removed:
            return False
        if day in self.added:
            return True
        if not (self.start <= day <= self.end):
            return False
        return day.weekday() in self.weekdays


@dataclass(frozen=True)
class Validity:
    """Whether an edge is usable on a given day under given conditions.

    All predicates are ANDed:
    - calendar: concrete date range (GTFS-style transit service); None = no
      date restriction.
    - open_months: annually-recurring open season as month numbers 1..12
      (e.g. {6,7,8,9,10} for a pass open Jun-Oct every year); empty = no month
      restriction. This is the right model for OSM seasonal road closures.
    - required_conditions: all must be present in the query's condition flags.
    - forbidden_conditions: all must be absent.
    """

    calendar: ServiceCalendar | None = None
    open_months: frozenset[int] = field(default_factory=frozenset)
    required_conditions: frozenset[str] = field(default_factory=frozenset)
    forbidden_conditions: frozenset[str] = field(default_factory=frozenset)

    def is_active(self, day: date,
                  conditions: frozenset[str] = frozenset()) -> bool:
        if self.calendar is not None and not self.calendar.runs_on(day):
            return False
        if self.open_months and day.month not in self.open_months:
            return False
        if not self.required_conditions <= conditions:
            return False
        if self.forbidden_conditions & conditions:
            return False
        return True


# Active on any date, under any conditions. Safe to share (immutable).
ALWAYS = Validity()


# Plain-dict (JSON-ready) serialization, shared by the road and scheduled
# artifacts. These return/accept built-in types only; the caller does the
# json.dump, so this module stays dependency-free.

def calendar_to_json(cal: ServiceCalendar | None):
    if cal is None:
        return None
    return {
        "start": cal.start.isoformat(),
        "end": cal.end.isoformat(),
        "weekdays": sorted(cal.weekdays),
        "added": [d.isoformat() for d in sorted(cal.added)],
        "removed": [d.isoformat() for d in sorted(cal.removed)],
    }


def calendar_from_json(obj) -> ServiceCalendar | None:
    if obj is None:
        return None
    return ServiceCalendar(
        start=date.fromisoformat(obj["start"]),
        end=date.fromisoformat(obj["end"]),
        weekdays=frozenset(obj["weekdays"]),
        added=frozenset(date.fromisoformat(d) for d in obj["added"]),
        removed=frozenset(date.fromisoformat(d) for d in obj["removed"]),
    )


def validity_to_json(v: Validity) -> dict:
    return {
        "calendar": calendar_to_json(v.calendar),
        "open_months": sorted(v.open_months),
        "required_conditions": sorted(v.required_conditions),
        "forbidden_conditions": sorted(v.forbidden_conditions),
    }


def validity_from_json(obj) -> Validity:
    return Validity(
        calendar=calendar_from_json(obj["calendar"]),
        open_months=frozenset(obj["open_months"]),
        required_conditions=frozenset(obj["required_conditions"]),
        forbidden_conditions=frozenset(obj["forbidden_conditions"]),
    )
