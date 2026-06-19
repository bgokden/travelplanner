"""Speed models: factors, time-of-day curve, active config, and routing effect."""

from datetime import datetime, timedelta

import pytest

from travelplanner.speed import (
    average_model,
    free_flow_model,
    get_speed_model,
    reset_speed_model,
    set_speed_model,
    time_of_day_model,
)


@pytest.fixture(autouse=True)
def _restore():
    yield
    reset_speed_model()


def _weekday_at(hour):
    d = datetime(2026, 6, 15, hour, 0)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def test_free_flow_is_one():
    assert free_flow_model("motorway", None) == 1.0
    assert free_flow_model("residential", _weekday_at(8)) == 1.0


def test_average_factors():
    m = average_model()
    assert m("motorway", None) == 1.05
    assert m("residential", None) == 1.82
    assert m(None, None) == 1.0          # unknown/empty class -> no slowdown
    assert m("not-a-class", None) == 1.0


def test_average_is_slower_than_free_flow():
    m = average_model()
    assert m("residential", None) > free_flow_model("residential", None)


def test_time_of_day_peak_slower_than_offpeak():
    tod = time_of_day_model()
    peak = tod("residential", _weekday_at(8))      # rush hour
    offpeak = tod("residential", _weekday_at(12))   # midday
    assert peak > offpeak == 1.82
    # highways congest less than urban streets at peak
    assert tod("motorway", _weekday_at(8)) < tod("residential", _weekday_at(8))


def test_time_of_day_never_beats_free_flow():
    tod = time_of_day_model()
    # a quiet night on a fast road must still be >= free-flow (clamped to 1.0)
    assert tod("motorway", _weekday_at(3)) >= 1.0


def test_time_of_day_without_depart_falls_back_to_base():
    tod = time_of_day_model()
    assert tod("residential", None) == average_model()("residential", None)


def test_active_model_config():
    assert get_speed_model()("residential", None) == 1.82   # no time -> average
    set_speed_model(free_flow_model)
    assert get_speed_model()("residential", None) == 1.0
    reset_speed_model()
    assert get_speed_model()("residential", None) == 1.82


def test_default_model_auto_applies_time_of_day():
    # The default decides automatically: same model, rush hour when a time is set.
    m = get_speed_model()
    assert m("residential", None) == 1.82                 # no time -> average
    assert m("residential", _weekday_at(8)) > 1.82        # peak -> slower, no opt-in


# --- routing effect (needs routingkit) ------------------------------------
routingkit = pytest.importorskip("routingkit_cch")

from travelplanner.graph.road import CCHRoadRouter, RoadGraphBuilder  # noqa: E402
from travelplanner.speed import average_model as _avg  # noqa: E402


def _resid_graph():
    b = RoadGraphBuilder(store_names=False)
    for k, lon in [("a", 9.50), ("b", 9.52), ("c", 9.55)]:
        b.add_node(k, 47.10, lon)
    b.add_road("a", "b", 100, highway="residential")
    b.add_road("b", "c", 100, highway="residential")
    return b.build()


def test_routing_average_slower_than_free_flow():
    from travelplanner.speed import free_flow_model as ff
    r = CCHRoadRouter(_resid_graph())
    day = datetime(2026, 6, 15).date()
    free = r.customize(day, speed_model=ff).route("a", "c").seconds
    avg = r.customize(day, speed_model=_avg()).route("a", "c").seconds
    assert free == 200                       # 2 x 100s free-flow
    assert avg == round(100 * 1.82) * 2      # residential factor applied
    assert avg > free


def test_routing_peak_slower_than_offpeak():
    r = CCHRoadRouter(_resid_graph())
    tod = time_of_day_model()
    peak = r.customize(_weekday_at(8).date(), depart_at=_weekday_at(8),
                       speed_model=tod).route("a", "c").seconds
    off = r.customize(_weekday_at(12).date(), depart_at=_weekday_at(12),
                      speed_model=tod).route("a", "c").seconds
    assert peak > off


class _StubCalendar:
    """Minimal calendar: one holiday date, school out for a date range."""
    def __init__(self, holiday=None, school_out_range=None):
        self.holiday = holiday
        self.school_out_range = school_out_range

    def is_holiday(self, day):
        return day == self.holiday

    def school_in_session(self, day):
        if day.weekday() >= 5 or day == self.holiday:
            return False
        if self.school_out_range:
            a, b = self.school_out_range
            return not (a <= day <= b)
        return True


def test_public_holiday_removes_peak():
    peak_dt = _weekday_at(8)
    cal = _StubCalendar(holiday=peak_dt.date())
    plain = time_of_day_model()
    holiday = time_of_day_model(calendar=cal)
    # normally rush hour is slow; on a public holiday the peak is gone
    assert plain("residential", peak_dt) > 1.82
    assert holiday("residential", peak_dt) == 1.82


def test_school_holiday_relieves_peak():
    peak_dt = _weekday_at(8)
    cal = _StubCalendar(school_out_range=(peak_dt.date(), peak_dt.date()))
    full_peak = time_of_day_model()("residential", peak_dt)
    relieved = time_of_day_model(calendar=cal,
                                 school_holiday_relief=0.5)("residential", peak_dt)
    assert 1.82 < relieved < full_peak     # lighter than term-time, still a peak


def test_holiday_calendar_package():
    pytest.importorskip("holidays")
    from datetime import date
    from travelplanner.speed import holiday_calendar
    cal = holiday_calendar("NL", school_holidays=[(date(2026, 7, 6), date(2026, 8, 16))])
    assert cal.is_holiday(date(2026, 12, 25))           # Christmas
    assert not cal.is_holiday(date(2026, 6, 15))
    assert cal.school_in_session(date(2026, 6, 15))     # a normal Monday
    assert not cal.school_in_session(date(2026, 7, 20))  # summer break
    assert not cal.school_in_session(date(2026, 12, 25))  # holiday -> not in session


def test_holiday_calendar_package_school_germany():
    pytest.importorskip("holidays")
    from datetime import date
    from travelplanner.speed import holiday_calendar
    # Germany exposes school holidays per Bundesland in the holidays package;
    # no user ranges needed.
    cal = holiday_calendar("DE", subdiv="BY")
    in_session = [d for d in (date(2026, 3, 9), date(2026, 3, 10), date(2026, 3, 11))
                  if cal.school_in_session(d)]
    out = [d for d in (date(2026, 1, 1), date(2026, 8, 5))
           if not cal.school_in_session(d)]
    assert in_session and out             # some days in session, some on break


def test_unclassed_graph_unaffected_by_average():
    # roads added without a highway class keep their explicit seconds
    b = RoadGraphBuilder(store_names=False)
    b.add_node("a", 47.1, 9.5)
    b.add_node("b", 47.12, 9.52)
    b.add_road("a", "b", 300)            # no highway -> class ""
    r = CCHRoadRouter(b.build())
    day = datetime(2026, 6, 15).date()
    assert r.customize(day, speed_model=_avg()).route("a", "b").seconds == 300
