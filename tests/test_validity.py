from datetime import date

from travelplanner.graph.validity import ALWAYS, ServiceCalendar, Validity

SUMMER = ServiceCalendar(
    start=date(2026, 6, 1),
    end=date(2026, 9, 30),
    weekdays=frozenset(range(7)),
)


def test_calendar_runs_in_season():
    assert SUMMER.runs_on(date(2026, 7, 15)) is True


def test_calendar_off_out_of_season():
    assert SUMMER.runs_on(date(2026, 1, 15)) is False


def test_calendar_weekday_mask():
    weekdays_only = ServiceCalendar(
        start=date(2026, 1, 1), end=date(2026, 12, 31),
        weekdays=frozenset({0, 1, 2, 3, 4}),
    )
    assert weekdays_only.runs_on(date(2026, 6, 19)) is True   # Friday
    assert weekdays_only.runs_on(date(2026, 6, 20)) is False  # Saturday


def test_removed_date_overrides_in_season():
    cal = ServiceCalendar(start=date(2026, 6, 1), end=date(2026, 9, 30),
                          removed=frozenset({date(2026, 7, 15)}))
    assert cal.runs_on(date(2026, 7, 15)) is False


def test_added_date_overrides_out_of_season():
    cal = ServiceCalendar(start=date(2026, 6, 1), end=date(2026, 9, 30),
                          added=frozenset({date(2026, 12, 25)}))
    assert cal.runs_on(date(2026, 12, 25)) is True


def test_always_is_active_any_date():
    assert ALWAYS.is_active(date(2026, 1, 1)) is True
    assert ALWAYS.is_active(date(2026, 8, 1), frozenset({"anything"})) is True


def test_required_conditions():
    v = Validity(required_conditions=frozenset({"pass_open"}))
    assert v.is_active(date(2026, 7, 1), frozenset({"pass_open"})) is True
    assert v.is_active(date(2026, 7, 1), frozenset()) is False


def test_forbidden_conditions():
    v = Validity(forbidden_conditions=frozenset({"storm"}))
    assert v.is_active(date(2026, 7, 1), frozenset()) is True
    assert v.is_active(date(2026, 7, 1), frozenset({"storm"})) is False


def test_calendar_and_conditions_combined():
    v = Validity(calendar=SUMMER, required_conditions=frozenset({"weather_good"}))
    assert v.is_active(date(2026, 7, 1), frozenset({"weather_good"})) is True
    assert v.is_active(date(2026, 1, 1), frozenset({"weather_good"})) is False
    assert v.is_active(date(2026, 7, 1), frozenset()) is False


def test_open_months_recurs_annually():
    # Pass open Jun-Oct, closed otherwise, every year.
    v = Validity(open_months=frozenset({6, 7, 8, 9, 10}))
    assert v.is_active(date(2026, 7, 15)) is True
    assert v.is_active(date(2027, 8, 1)) is True   # next year, still open
    assert v.is_active(date(2026, 1, 15)) is False
    assert v.is_active(date(2026, 11, 1)) is False
