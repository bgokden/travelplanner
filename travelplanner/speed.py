"""Pluggable driving-speed models: free-flow, average, and time-of-day.

A speed model maps a road's highway class and the departure time to a TIME
MULTIPLIER on its free-flow travel time (the time implied by the speed limit):

    SpeedModel = (highway_class | None, depart_at | None) -> float
    1.0 = free-flow (best case);  >1.0 = slower than free-flow.

Arcs keep their free-flow `base_seconds`; the model is applied at customization,
once per distinct highway class, so the same road artifact serves any profile and
any departure time with no rebuild. The default decides automatically: it is
time-of-day aware, behaving as plain average when no departure time is given and
applying a rush-hour curve when one is -- the caller never selects a model, and
free-flow (the dangerous best case) is opt-in only.

These defaults are typical-day heuristics, not live traffic: they model "a normal
Tuesday at 8am", not today's accident. A data-backed model (returning
free_flow_time / predicted_time per class) is just another SpeedModel.
"""

from datetime import datetime
from typing import Callable, Optional

SpeedModel = Callable[[Optional[str], Optional[datetime]], float]

# Highway classes that flow near the limit (vs urban/arterial streets).
HIGHWAY_CLASSES = frozenset({"motorway", "motorway_link", "trunk", "trunk_link"})

# Classes whose travel time is fixed, not a road speed (e.g. a ferry crossing,
# timed by its OSM `duration` tag): road congestion must never scale it.
FIXED_TIME_CLASSES = frozenset({"ferry"})

# Average time multiplier vs free-flow per class (1 / typical achieved fraction).
AVERAGE_FACTORS = {
    "motorway": 1.05, "motorway_link": 1.10,
    "trunk": 1.11, "trunk_link": 1.18,
    "primary": 1.33, "primary_link": 1.40,
    "secondary": 1.43, "secondary_link": 1.50,
    "tertiary": 1.54, "tertiary_link": 1.60,
    "unclassified": 1.67, "residential": 1.82, "living_street": 2.00,
    "service": 2.00, "road": 1.54, "track": 2.00,
}


def free_flow_model(highway: Optional[str], depart_at: Optional[datetime]) -> float:
    """Best case: travel at the speed limit (multiplier 1.0). Opt-in."""
    return 1.0


def average_model(factors: Optional[dict] = None) -> SpeedModel:
    """Constant per-class slowdown reflecting typical conditions (the default)."""
    table = AVERAGE_FACTORS if factors is None else factors

    def model(highway: Optional[str], depart_at: Optional[datetime]) -> float:
        return table.get(highway or "", 1.0)
    return model


def _time_of_day_factor(highway: Optional[str], dt: datetime, *,
                        peak_urban: float, peak_highway: float,
                        night: float) -> float:
    if highway in FIXED_TIME_CLASSES:
        return 1.0          # a fixed crossing time, not a congesting road
    is_highway = highway in HIGHWAY_CLASSES
    hour = dt.hour
    if dt.weekday() < 5:  # weekday
        if hour in (7, 8, 16, 17, 18):                 # rush hours
            return peak_highway if is_highway else peak_urban
        if hour >= 22 or hour <= 5:                     # night
            return night
        return 1.0
    # weekend: a mild midday bump, quiet nights
    if 11 <= hour <= 17:
        return 1.05 if is_highway else 1.15
    if hour >= 22 or hour <= 6:
        return night
    return 1.0


def _is_holiday(calendar, day) -> bool:
    fn = getattr(calendar, "is_holiday", None)
    return bool(fn(day)) if fn is not None else False


def _school_in_session(calendar, day) -> bool:
    fn = getattr(calendar, "school_in_session", None)
    return bool(fn(day)) if fn is not None else True


def time_of_day_model(base: Optional[SpeedModel] = None, *,
                      peak_urban: float = 1.45, peak_highway: float = 1.20,
                      night: float = 0.95, calendar=None,
                      school_holiday_relief: float = 0.5) -> SpeedModel:
    """Average (or `base`) scaled by a heuristic hour/weekday congestion curve.

    With an optional `calendar` (see holiday_calendar) the curve also reacts to
    the date: a public holiday collapses the commute (no rush-hour peak), and a
    school holiday on a workday lightens the peak by `school_holiday_relief`
    (0 = no relief, 1 = peak removed). The combined multiplier is clamped to
    >= 1.0 so a quiet hour never beats the free-flow speed limit. With no
    depart_at it falls back to `base`.
    """
    base_model = base if base is not None else average_model()

    def model(highway: Optional[str], depart_at: Optional[datetime]) -> float:
        b = base_model(highway, depart_at)
        if depart_at is None:
            return b
        day = depart_at.date()
        if calendar is not None and _is_holiday(calendar, day):
            hour = depart_at.hour
            return max(1.0, b * (night if hour >= 22 or hour <= 5 else 1.0))
        factor = _time_of_day_factor(highway, depart_at, peak_urban=peak_urban,
                                     peak_highway=peak_highway, night=night)
        if (factor > 1.0 and calendar is not None
                and not _school_in_session(calendar, day)):
            factor = 1.0 + (factor - 1.0) * school_holiday_relief
        return max(1.0, b * factor)
    return model


def holiday_calendar(country: str, subdiv: Optional[str] = None, *,
                     school_holidays=None, years=None,
                     use_package_school: bool = True):
    """Date calendar backed by the `holidays` package (a core dependency).

    Public holidays come from `holidays.country_holidays(country, subdiv=...)`.

    School holidays are resolved in priority order:
    1. an explicit `school_holidays` -- an iterable of (start, end) inclusive
       date ranges, or a callable `date -> bool` (True when school is OUT);
    2. else, if `use_package_school`, the holidays package's SCHOOL category for
       this country/subdivision when it has data (e.g. Germany per Bundesland --
       pass subdiv="BY"; coverage elsewhere is sparse);
    3. else school is assumed always in session.

    The holidays package's school coverage is uneven (strong for Germany, absent
    for e.g. NL/FR/GB), so supply ranges for those, or use an external source.
    Pass the result as `calendar=` to time_of_day_model.
    """
    import holidays as _holidays  # imported lazily to keep importing speed light

    public = _holidays.country_holidays(country, subdiv=subdiv, years=years)

    if callable(school_holidays):
        school_out = school_holidays
    elif school_holidays is not None:
        ranges = list(school_holidays)

        def school_out(day) -> bool:
            return any(start <= day <= end for start, end in ranges)
    elif use_package_school:
        try:
            school = _holidays.country_holidays(
                country, subdiv=subdiv, years=years, categories=("school",))
        except (ValueError, KeyError, NotImplementedError):
            school = None

        def school_out(day) -> bool:
            return school is not None and day in school
    else:
        def school_out(day) -> bool:
            return False

    class _Calendar:
        def is_holiday(self, day) -> bool:
            return day in public

        def school_in_session(self, day) -> bool:
            return day.weekday() < 5 and day not in public and not school_out(day)

    return _Calendar()


# Default: time-of-day aware. It decides automatically -- with no departure time
# it behaves as plain average; given a depart_at it applies the rush-hour curve.
# The caller never has to choose a model.
_active: SpeedModel = time_of_day_model()


def set_speed_model(model: SpeedModel) -> None:
    """Set the active speed model used by drive()/customized() by default."""
    global _active
    _active = model


def get_speed_model() -> SpeedModel:
    """The active speed model (default: time_of_day_model())."""
    return _active


def reset_speed_model() -> None:
    """Restore the default time-of-day speed model."""
    global _active
    _active = time_of_day_model()
