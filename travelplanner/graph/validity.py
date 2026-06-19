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
