"""Auto-composed timetable resolver, with the network monkeypatched out."""

from datetime import datetime

from travelplanner import auto_timetable
from travelplanner.transit_catalog import Feed
from travelplanner.models import Location, LocationType, Mode
from travelplanner.graph.scheduled import (
    ConnectionScan, Stop, Timetable, make_trip)

O = Location("O", LocationType.CITY, 52.37, 4.90)
D = Location("D", LocationType.CITY, 52.34, 4.88)


def _feed(fid, min_lat, min_lon, max_lat, max_lon):
    return Feed(id=fid, name=f"F{fid}", provider="P", country="NL",
                url="http://x/f.zip", min_lat=min_lat, min_lon=min_lon,
                max_lat=max_lat, max_lon=max_lon)


def _ground_tt():
    tt = Timetable()
    tt.add_stop(Stop("A", "A", 52.37, 4.90))
    tt.add_stop(Stop("B", "B", 52.34, 4.88))
    tt.add_trip(make_trip("GT", Mode.TRAIN, [
        ("A", "09:00", "09:00"), ("B", "09:20", "09:20")]))
    return tt


def test_merges_a_covering_ground_feed(monkeypatch):
    feed = _feed("1", 52.0, 4.0, 53.0, 5.0)            # bbox covers both points
    monkeypatch.setattr(auto_timetable, "catalog", lambda: {"1": feed})
    monkeypatch.setattr(auto_timetable, "_load_feed", lambda f: _ground_tt())
    tt, notes = auto_timetable.build_default_timetable(O, D, air=False)
    assert "GT" in tt.trips and {"A", "B"} <= set(tt.stops)
    assert notes == []


def test_notes_when_no_feed_covers(monkeypatch):
    monkeypatch.setattr(auto_timetable, "catalog", lambda: {})
    tt, notes = auto_timetable.build_default_timetable(O, D, air=False)
    assert tt.trips == {}
    assert any("no GTFS feed" in n for n in notes)


def test_notes_when_feed_fetch_fails(monkeypatch):
    feed = _feed("1", 52.0, 4.0, 53.0, 5.0)
    monkeypatch.setattr(auto_timetable, "catalog", lambda: {"1": feed})

    def boom(f):
        raise OSError("network down")

    monkeypatch.setattr(auto_timetable, "_load_feed", boom)
    tt, notes = auto_timetable.build_default_timetable(O, D, air=False)
    assert tt.trips == {}
    assert any("unavailable" in n for n in notes)


def test_includes_air_scoped_to_nearby_airports(monkeypatch):
    monkeypatch.setattr(auto_timetable, "catalog", lambda: {})
    monkeypatch.setattr(auto_timetable, "airports_near",
                        lambda pts, r, download: {"AAA", "BBB"})
    monkeypatch.setattr(auto_timetable, "hub_airports",
                        lambda pts, r, min_routes, download: set())
    flights = Timetable()
    flights.add_stop(Stop("AAA", "AAA", 52.3, 4.7, tz="Europe/Amsterdam"))
    flights.add_stop(Stop("BBB", "BBB", 47.4, 8.5, tz="Europe/Zurich"))
    flights.add_trip(make_trip("AAA-BBB", Mode.FLIGHT, [
        ("AAA", "10:00", "10:00"), ("BBB", "11:30", "11:30")]))
    monkeypatch.setattr(auto_timetable, "load_openflights",
                        lambda keep, download: flights)
    tt, notes = auto_timetable.build_default_timetable(O, D, ground=False)
    assert "AAA-BBB" in tt.trips


def test_air_keep_includes_connection_hubs(monkeypatch):
    """The flight network keeps near-endpoint airports AND nearby hub airports, so a
    trip with no direct flight routes origin -> hub -> destination."""
    monkeypatch.setattr(auto_timetable, "catalog", lambda: {})
    monkeypatch.setattr(auto_timetable, "airports_near",
                        lambda pts, r, download: {"NO", "ND"})    # near origin/dest
    monkeypatch.setattr(auto_timetable, "hub_airports",
                        lambda pts, r, min_routes, download: {"HUB"})
    captured = {}

    def fake_load(keep, download):
        captured["keep"] = set(keep)
        tt = Timetable()
        tt.add_stop(Stop("NO", "NO", 52.3, 4.7))
        tt.add_stop(Stop("HUB", "HUB", 50.0, 8.5))
        tt.add_stop(Stop("ND", "ND", 47.4, 8.5))
        tt.add_trip(make_trip("NO-HUB", Mode.FLIGHT, [
            ("NO", "09:00", "09:00"), ("HUB", "10:00", "10:00")]))
        tt.add_trip(make_trip("HUB-ND", Mode.FLIGHT, [
            ("HUB", "11:00", "11:00"), ("ND", "12:00", "12:00")]))
        return tt

    monkeypatch.setattr(auto_timetable, "load_openflights", fake_load)
    tt, notes = auto_timetable.build_default_timetable(O, D, ground=False)
    assert captured["keep"] == {"NO", "ND", "HUB"}        # hub added to near airports
    # The connection routes end to end through the auto-composed network.
    j = ConnectionScan(tt).query({"NO": datetime(2026, 6, 23, 8, 0)}, "ND")
    assert j is not None
    assert [leg.from_stop for leg in j.legs] == ["NO", "HUB"]
    assert j.legs[-1].to_stop == "ND"


def test_no_air_when_too_few_nearby_airports(monkeypatch):
    monkeypatch.setattr(auto_timetable, "catalog", lambda: {})
    monkeypatch.setattr(auto_timetable, "airports_near",
                        lambda pts, r, download: {"AAA"})       # only one
    tt, notes = auto_timetable.build_default_timetable(O, D, ground=False)
    assert tt.trips == {}
    assert any("no airports near" in n for n in notes)


def test_catalog_download_failure_degrades_to_note(monkeypatch):
    def boom():
        raise OSError("network down")
    monkeypatch.setattr(auto_timetable, "catalog", boom)
    tt, notes = auto_timetable.build_default_timetable(O, D, air=False)
    assert tt.trips == {}
    assert any("catalog unavailable" in n for n in notes)       # no crash


def test_air_download_failure_degrades_to_note(monkeypatch):
    from urllib.error import URLError

    def boom(pts, r, download):
        raise URLError("no network")                            # OSError subclass
    monkeypatch.setattr(auto_timetable, "airports_near", boom)
    monkeypatch.setattr(auto_timetable, "catalog", lambda: {})
    tt, notes = auto_timetable.build_default_timetable(O, D)
    assert any("flight network unavailable" in n for n in notes)  # no crash
