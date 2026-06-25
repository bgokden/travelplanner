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


def test_merges_curated_national_rail_for_german_trip(monkeypatch):
    # The catalog carries no national rail feed, so a curated publisher feed is merged
    # for a trip touching its country box (Germany), supplying the intercity train the
    # catalog's regional feeds lack.
    muc = Location("Munich", LocationType.CITY, 48.14, 11.56)
    nbg = Location("Nuremberg", LocationType.CITY, 49.45, 11.08)
    monkeypatch.setattr(auto_timetable, "catalog", lambda: {})
    monkeypatch.setattr(auto_timetable, "feeds_for_trip",
                        lambda o, d, catalog=None: [])

    def load(feed):
        assert feed.country == "DE"                     # only the curated DE feed loads
        tt = Timetable()
        tt.add_stop(Stop("MUC", "Munich Hbf", 48.14, 11.56))
        tt.add_stop(Stop("NBG", "Nuremberg Hbf", 49.45, 11.08))
        tt.add_trip(make_trip("ICE", Mode.TRAIN, [
            ("MUC", "09:00", "09:00"), ("NBG", "10:00", "10:00")]))
        return tt

    monkeypatch.setattr(auto_timetable, "_load_feed", load)
    tt, notes = auto_timetable.build_default_timetable(muc, nbg, air=False)
    assert "ICE" in tt.trips and {"MUC", "NBG"} <= set(tt.stops)   # curated rail merged
    assert not any("curated" in n for n in notes)                  # it loaded cleanly


def test_curated_rail_skipped_outside_its_country(monkeypatch):
    # A trip with no endpoint inside a curated feed's box does not fetch it (O/D are in
    # Amsterdam, west of the German box).
    loaded = []
    monkeypatch.setattr(auto_timetable, "catalog", lambda: {})
    monkeypatch.setattr(auto_timetable, "feeds_for_trip",
                        lambda o, d, catalog=None: [])
    monkeypatch.setattr(auto_timetable, "_load_feed",
                        lambda f: loaded.append(f.id) or Timetable())
    auto_timetable.build_default_timetable(O, D, air=False)
    assert loaded == []                                  # no curated feed fetched


def test_skips_dead_feed_and_uses_next_covering_feed(monkeypatch):
    """A feed whose download fails (a dead catalog URL) must not leave the trip with
    no ground transit: the next covering feed is tried instead."""
    dead = _feed("1", 52.2, 4.7, 52.5, 5.0)            # smaller bbox -> tried first
    good = _feed("2", 52.0, 4.0, 53.0, 5.0)            # larger bbox -> tried next
    monkeypatch.setattr(auto_timetable, "catalog", lambda: {"1": dead, "2": good})

    def load(f):
        if f.id == "1":
            raise OSError("dead url")
        return _ground_tt()

    monkeypatch.setattr(auto_timetable, "_load_feed", load)
    tt, notes = auto_timetable.build_default_timetable(O, D, air=False)
    assert "GT" in tt.trips                             # reached the working feed
    assert any("unavailable" in n for n in notes)       # noted the dead one


def test_skips_feed_with_no_corridor_service(monkeypatch):
    """A feed whose bbox covers the trip but whose stops fall outside the corridor
    contributes nothing; it is skipped (not counted) and the next feed is tried."""
    off = _feed("1", 52.2, 4.7, 52.5, 5.0)             # tried first, stops are far
    good = _feed("2", 52.0, 4.0, 53.0, 5.0)            # tried next, serves corridor
    monkeypatch.setattr(auto_timetable, "catalog", lambda: {"1": off, "2": good})

    def load(f):
        if f.id == "1":
            far = Timetable()
            far.add_stop(Stop("FA", "FA", 10.0, 10.0))
            far.add_stop(Stop("FB", "FB", 10.1, 10.1))
            far.add_trip(make_trip("FAR", Mode.TRAIN, [
                ("FA", "09:00", "09:00"), ("FB", "09:20", "09:20")]))
            return far
        return _ground_tt()

    monkeypatch.setattr(auto_timetable, "_load_feed", load)
    tt, notes = auto_timetable.build_default_timetable(O, D, air=False)
    assert "GT" in tt.trips                             # reached the serving feed
    assert "FAR" not in tt.trips                        # off-corridor feed skipped
    assert any("no service in the trip corridor" in n for n in notes)


def test_merges_dense_feed_alongside_sparse_small_box_feed(monkeypatch):
    """A sparse operator with a smaller bounding box must not shadow the dense feed
    that carries the real through-service: both are merged (within the budget), so
    the planner sees the dense feed's connection, not just the smallest-box one."""
    far_o = Location("O", LocationType.CITY, 52.37, 4.90)   # Amsterdam-ish
    far_d = Location("D", LocationType.CITY, 52.50, 13.40)  # Berlin-ish
    sparse = _feed("sparse", 52.0, 4.0, 53.0, 14.0)         # smaller box -> first
    dense = _feed("dense", 51.0, 3.0, 54.0, 15.0)           # bigger box -> second
    monkeypatch.setattr(auto_timetable, "catalog",
                        lambda: {"sparse": sparse, "dense": dense})

    def load(f):
        tt = Timetable()
        tt.add_stop(Stop("A", "A", 52.37, 4.90))
        tt.add_stop(Stop("Z", "Z", 52.50, 13.40))
        tt.add_trip(make_trip("SPARSE" if f.id == "sparse" else "DENSE", Mode.TRAIN,
                              [("A", "09:00", "09:00"), ("Z", "15:00", "15:00")]))
        return tt

    monkeypatch.setattr(auto_timetable, "_load_feed", load)
    tt, notes = auto_timetable.build_default_timetable(far_o, far_d, air=False)
    assert "SPARSE" in tt.trips and "DENSE" in tt.trips   # both, not just smallest box


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
