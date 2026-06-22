"""Timezone-aware connection materialization (UTC internally, local at the edge)."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from travelplanner import plan
from travelplanner.models import CostLevel, Location, LocationType, Mode
from travelplanner.graph.coupling import GeometricConnector
from travelplanner.graph.scheduled import ConnectionScan, Stop, Timetable, make_trip

UTC = timezone.utc
NY = ZoneInfo("America/New_York")
AMS = ZoneInfo("Europe/Amsterdam")
ZRH = ZoneInfo("Europe/Zurich")


def test_international_flight_materializes_in_utc():
    # Stop-time offsets are wall-clock in each stop's own zone: depart 12:00 local
    # New York (16:00 UTC in July), arrive 22:00 local Amsterdam (20:00 UTC).
    tt = Timetable()
    tt.add_stop(Stop("JFK", "New York JFK", 40.64, -73.78, tz="America/New_York"))
    tt.add_stop(Stop("AMS", "Amsterdam", 52.31, 4.77, tz="Europe/Amsterdam"))
    tt.add_trip(make_trip("FL1", Mode.FLIGHT, [
        ("JFK", "12:00", "12:00"), ("AMS", "22:00", "22:00")],
        cost_level=CostLevel.HIGH))
    src = datetime(2026, 7, 1, 6, 0, tzinfo=NY)        # aware seed, before departure
    j = ConnectionScan(tt).query({"JFK": src}, "AMS")
    assert j is not None
    assert j.depart == datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
    assert j.arrive == datetime(2026, 7, 1, 20, 0, tzinfo=UTC)
    # Rendered in each leg's local zone: leave 12:00 NYC, land 22:00 AMS.
    assert j.depart.astimezone(NY).strftime("%H:%M") == "12:00"
    assert j.arrive.astimezone(AMS).strftime("%H:%M") == "22:00"
    # A real 4h hop, not the naive 10h the wall-clock numbers would suggest.
    assert j.arrive - j.depart == timedelta(hours=4)


def test_single_zone_feed_round_trips_local_clock():
    tt = Timetable()
    tt.add_stop(Stop("A", "A", 47.0, 8.0, tz="Europe/Zurich"))
    tt.add_stop(Stop("B", "B", 47.5, 8.5, tz="Europe/Zurich"))
    tt.add_trip(make_trip("T", Mode.TRAIN, [
        ("A", "09:00", "09:00"), ("B", "10:00", "10:00")]))
    j = ConnectionScan(tt).query(
        {"A": datetime(2026, 7, 1, 8, 0, tzinfo=ZRH)}, "B")
    assert j is not None
    assert j.arrive == datetime(2026, 7, 1, 8, 0, tzinfo=UTC)   # 10:00 CEST
    assert j.arrive.astimezone(ZRH).strftime("%H:%M") == "10:00"


def test_dst_offset_is_applied_at_the_wall_time():
    # Same local 09:00 Zurich departure, winter (UTC+1) vs summer (UTC+2):
    # the materialized UTC instant differs by the DST offset.
    tt = Timetable()
    tt.add_stop(Stop("A", "A", 47.0, 8.0, tz="Europe/Zurich"))
    tt.add_stop(Stop("B", "B", 47.5, 8.5, tz="Europe/Zurich"))
    tt.add_trip(make_trip("T", Mode.TRAIN, [
        ("A", "09:00", "09:00"), ("B", "09:30", "09:30")]))
    csa = ConnectionScan(tt, horizon=timedelta(hours=6))
    summer = csa.query({"A": datetime(2026, 7, 1, 7, 0, tzinfo=ZRH)}, "B")
    winter = csa.query({"A": datetime(2026, 1, 15, 7, 0, tzinfo=ZRH)}, "B")
    assert summer.depart == datetime(2026, 7, 1, 7, 0, tzinfo=UTC)    # CEST UTC+2
    assert winter.depart == datetime(2026, 1, 15, 8, 0, tzinfo=UTC)   # CET  UTC+1


def test_plan_over_tz_aware_feed_reads_depart_as_origin_local():
    # Regression: once load_timetable captures tz, plan() over a tz-aware feed
    # must not mix naive access legs with UTC transit legs (a crash); a naive
    # depart_at is read as local at the origin, so a single-zone trip's displayed
    # clock is unchanged from the naive-feed behavior.
    tt = Timetable()
    tt.add_stop(Stop("A", "A", 47.0, 8.0, tz="Europe/Zurich"))
    tt.add_stop(Stop("B", "B", 47.5, 8.6, tz="Europe/Zurich"))
    tt.add_trip(make_trip("T", Mode.TRAIN, [
        ("A", "09:00", "09:00"), ("B", "10:00", "10:00")]))
    origin = Location("Origin", LocationType.HOTEL, 47.0, 8.0)
    dest = Location("Dest", LocationType.HOTEL, 47.5, 8.6)
    res = plan(origin, dest, datetime(2026, 7, 1, 8, 0), tt,
               GeometricConnector(tt.stops))
    assert res                                          # plans, does not crash
    it = res[0]
    assert it.depart_at.tzinfo is not None              # aware for a tz feed
    assert it.depart_at.utcoffset() == timedelta(hours=2)   # CEST origin-local
    assert it.depart_at.strftime("%H:%M") == "08:00"   # same wall clock as input


def test_aware_depart_over_naive_feed_does_not_crash():
    # Regression: a naive (no-timezone) feed with an AWARE departure must not
    # raise on a naive-vs-aware comparison; the tz is shed, the wall clock kept.
    tt = Timetable()
    tt.add_stop(Stop("A", "A", 47.0, 8.0))            # no tz -> naive feed
    tt.add_stop(Stop("B", "B", 47.5, 8.6))
    tt.add_trip(make_trip("T", Mode.TRAIN, [
        ("A", "09:00", "09:00"), ("B", "10:00", "10:00")]))
    origin = Location("O", LocationType.HOTEL, 47.0, 8.0)
    dest = Location("D", LocationType.HOTEL, 47.5, 8.6)
    aware = datetime(2026, 7, 1, 8, 0, tzinfo=UTC)
    assert plan(origin, dest, aware, tt, GeometricConnector(tt.stops))  # no crash
    # CSA level too: aware sources over a naive timetable resolve in naive time.
    j = ConnectionScan(tt).query({"A": aware}, "B")
    assert j is not None and j.arrive == datetime(2026, 7, 1, 10, 0)


def test_international_itinerary_legs_carry_local_zones():
    # A transatlantic flight: each leg endpoint carries its own zone so the
    # output can render leave-Amsterdam / land-New-York in local time.
    tt = Timetable()
    tt.add_stop(Stop("AMS", "Schiphol", 52.31, 4.77, tz="Europe/Amsterdam"))
    tt.add_stop(Stop("JFK", "New York JFK", 40.64, -73.78, tz="America/New_York"))
    tt.add_trip(make_trip("FL", Mode.FLIGHT, [
        ("AMS", "10:00", "10:00"), ("JFK", "12:00", "12:00")],
        cost_level=CostLevel.HIGH))
    origin = Location("Amsterdam", LocationType.CITY, 52.30, 4.76)
    dest = Location("New York", LocationType.CITY, 40.65, -73.79)
    res = plan(origin, dest, datetime(2026, 7, 1, 8, 0), tt,
               GeometricConnector(tt.stops))
    flight = next(it for it in res if any(l.mode is Mode.FLIGHT for l in it.legs))
    legs = flight.legs
    assert legs[0].from_loc.tz == "Europe/Amsterdam"      # origin door, origin zone
    assert legs[-1].to_loc.tz == "America/New_York"        # dest door, dest zone
    fl = next(l for l in legs if l.mode is Mode.FLIGHT)
    assert fl.from_loc.tz == "Europe/Amsterdam"
    assert fl.to_loc.tz == "America/New_York"
    # Arrival renders ~noon in New York local, not in the Amsterdam origin zone.
    assert flight.arrive_at.astimezone(NY).strftime("%H") == "12"
