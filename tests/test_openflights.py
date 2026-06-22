"""Loading a synthetic flight Timetable from OpenFlights-format data (no network)."""

from datetime import datetime, timedelta

from travelplanner.models import Mode, CostLevel
from travelplanner.openflights import (hub_airports, load_airports,
                                       load_flight_network, load_openflights,
                                       search_airports)
from travelplanner.graph.schema import NodeType
from travelplanner.graph.scheduled import ConnectionScan

# OpenFlights airports.dat rows (no header): id,name,city,country,IATA,ICAO,
# lat,lon,alt,tz,dst,tzdb,type,source. AMS, ZRH, plus one without an IATA code.
AIRPORTS = '''\
580,"Amsterdam Schiphol","Amsterdam","Netherlands","AMS","EHAM",52.3105,4.7683,-11,1,"E","Europe/Amsterdam","airport","OurAirports"
1678,"Zurich","Zurich","Switzerland","ZRH","LSZH",47.4647,8.5492,1416,1,"E","Europe/Zurich","airport","OurAirports"
9999,"No IATA Field","Nowhere","Nowhere","\\N","XXXX",10.0,10.0,0,0,"U","\\N","airport","OurAirports"
'''

# routes.dat rows (no header): airline,airlineID,src,srcID,dst,dstID,codeshare,
# stops,equipment. A non-stop AMS-ZRH (twice, codeshare), one 1-stop (dropped),
# and one referencing the IATA-less airport (dropped).
ROUTES = '''\
KL,3090,AMS,580,ZRH,1678,,0,738
LX,4559,AMS,580,ZRH,1678,Y,0,32A
KL,3090,ZRH,1678,AMS,580,,0,738
XX,1,AMS,580,ZRH,1678,,1,738
YY,2,AMS,580,XXX,9999,,0,738
'''


def _feed(tmp_path):
    a = tmp_path / "airports.dat"
    r = tmp_path / "routes.dat"
    a.write_text(AIRPORTS, encoding="utf-8")
    r.write_text(ROUTES, encoding="utf-8")
    return str(a), str(r)


def test_loads_airports_and_directed_routes(tmp_path):
    a, r = _feed(tmp_path)
    tt = load_openflights(a, r, depart_hours=(8,))
    # only airports with an IATA code AND a flight are kept
    assert set(tt.stops) == {"AMS", "ZRH"}
    assert all(s.type is NodeType.AIRPORT for s in tt.stops.values())
    # two directed non-stop pairs (AMS->ZRH dedups the codeshare; ZRH->AMS),
    # one trip each at the single departure hour
    assert len(tt.trips) == 2
    assert all(t.mode is Mode.FLIGHT and t.cost_level is CostLevel.HIGH
               for t in tt.trips.values())


def test_airport_timezone_is_captured(tmp_path):
    a, r = _feed(tmp_path)
    tt = load_openflights(a, r, depart_hours=(8,))
    # Column 11 (tz database name) becomes Stop.tz, so synthetic local departure
    # hours can later be anchored to the airport's real timezone.
    assert tt.stops["AMS"].tz == "Europe/Amsterdam"
    assert tt.stops["ZRH"].tz == "Europe/Zurich"


def test_invalid_or_null_airport_timezone_becomes_none(tmp_path):
    # OpenFlights writes "\\N" for an unknown zone; a feed can also carry a
    # bogus name. Both must clean to None, not crash connection materialization.
    airports = (
        '1,"A","CityA","X","AAA","AAAA",10.0,10.0,0,0,"U","\\N","airport","src"\n'
        '2,"B","CityB","X","BBB","BBBB",11.0,11.0,0,0,"U","Not/AZone","airport","src"\n'
    )
    routes = "ZZ,1,AAA,1,BBB,2,,0,738\nZZ,1,BBB,2,AAA,1,,0,738\n"
    a = tmp_path / "airports.dat"
    a.write_text(airports, encoding="utf-8")
    r = tmp_path / "routes.dat"
    r.write_text(routes, encoding="utf-8")
    tt = load_openflights(str(a), str(r), depart_hours=(8,))
    assert set(tt.stops) == {"AAA", "BBB"}
    assert tt.stops["AAA"].tz is None        # "\\N" null marker
    assert tt.stops["BBB"].tz is None        # unloadable zone name


def test_one_stop_and_unknown_airport_routes_dropped(tmp_path):
    a, r = _feed(tmp_path)
    tt = load_openflights(a, r, depart_hours=(8,))
    # the 1-stop AMS-ZRH and the AMS-XXX (unknown airport) rows produce no trips
    pairs = {(t.stop_times[0].stop_id, t.stop_times[1].stop_id)
             for t in tt.trips.values()}
    assert pairs == {("AMS", "ZRH"), ("ZRH", "AMS")}


def test_multiple_departures_and_synthetic_duration(tmp_path):
    a, r = _feed(tmp_path)
    tt = load_openflights(a, r, depart_hours=(6, 18), cruise_kmh=800.0,
                          overhead=timedelta(minutes=45))
    # 2 directed pairs x 2 departures = 4 trips
    assert len(tt.trips) == 4
    # AMS-ZRH great-circle ~600 km -> ~45 min over + ~45 min cruise ~= 1h30
    amsz = next(t for t in tt.trips.values()
                if t.stop_times[0].stop_id == "AMS" and t.id.endswith("@06"))
    dur = amsz.stop_times[1].arrival - amsz.stop_times[0].departure
    assert timedelta(minutes=60) < dur < timedelta(minutes=120)


def test_keep_filters_airports(tmp_path):
    a, r = _feed(tmp_path)
    tt = load_openflights(a, r, keep={"AMS"}, depart_hours=(8,))
    # with only AMS kept, no route has both endpoints -> empty network
    assert tt.stops == {} and tt.trips == {}


def test_routes_into_a_real_journey(tmp_path):
    """The synthesized flights actually route end to end via CSA."""
    a, r = _feed(tmp_path)
    tt = load_openflights(a, r, depart_hours=(8,))
    j = ConnectionScan(tt).query({"AMS": datetime(2026, 7, 1, 6, 0)}, "ZRH")
    assert j is not None
    assert j.legs[0].mode is Mode.FLIGHT
    assert j.legs[0].from_stop == "AMS" and j.legs[-1].to_stop == "ZRH"


def test_requires_paths_or_download(tmp_path):
    import pytest
    with pytest.raises(ValueError):
        load_openflights()                       # no paths, no download


def test_load_and_search_airports(tmp_path):
    a, _ = _feed(tmp_path)
    rows = load_airports(str(a))
    codes = {r["iata"] for r in rows}
    assert codes == {"AMS", "ZRH"}               # the IATA-less row is skipped
    ams = next(r for r in rows if r["iata"] == "AMS")
    assert ams["city"] == "Amsterdam" and ams["country"] == "Netherlands"

    # exact IATA match ranks first; name/city prefixes also match
    assert search_airports("zrh", airports=rows)[0]["iata"] == "ZRH"
    assert search_airports("schip", airports=rows)[0]["iata"] == "AMS"
    assert search_airports("z", airports=rows) == []        # too short
    assert search_airports("nomatch", airports=rows) == []


def test_load_flight_network_honors_supplied_airports_with_cached_routes(
        tmp_path, monkeypatch):
    # Regression: passing airports= with routes=None must honor the supplied
    # airports path, not raise "not cached" because the cache airports file is
    # absent. Cache holds only routes; airports is supplied explicitly.
    import travelplanner.roads as roads
    a, _ = _feed(tmp_path)
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "openflights-routes.dat").write_text(ROUTES, encoding="utf-8")
    monkeypatch.setattr(roads, "cache_dir", lambda: str(cache))
    tt = load_flight_network(airports=a, routes=None, min_routes=1,
                             depart_hours=(8,))
    assert "AMS" in tt.stops and "ZRH" in tt.stops


def test_load_flight_network_filters_by_degree(tmp_path):
    a, r = _feed(tmp_path)
    # min_routes too high -> no hubs kept -> empty network
    assert load_flight_network(airports=a, routes=r, min_routes=99).stops == {}
    # low threshold keeps AMS/ZRH and their flights
    tt = load_flight_network(airports=a, routes=r, min_routes=1, depart_hours=(8,))
    assert "AMS" in tt.stops and "ZRH" in tt.stops
    assert tt.trips and all(t.mode is Mode.FLIGHT for t in tt.trips.values())


# Three airports: HUB (degree 3) and FAR (degree 2) are hubs; LOW (degree 1) is
# not. HUB and LOW are co-located; FAR is on the far side of the world.
HUB_AIRPORTS = '''\
1,"Hub","HubCity","X","HUB","HUBB",50.0,8.0,0,0,"U","\\N","airport","src"
2,"Low","LowCity","X","LOW","LOWW",50.1,8.1,0,0,"U","\\N","airport","src"
3,"Far","FarCity","X","FAR","FARR",10.0,100.0,0,0,"U","\\N","airport","src"
'''
HUB_ROUTES = '''\
ZZ,1,HUB,1,LOW,2,,0,738
ZZ,1,HUB,1,FAR,3,,0,738
ZZ,1,FAR,3,HUB,1,,0,738
'''


def _hub_feed(tmp_path):
    a = tmp_path / "airports.dat"
    a.write_text(HUB_AIRPORTS, encoding="utf-8")
    r = tmp_path / "routes.dat"
    r.write_text(HUB_ROUTES, encoding="utf-8")
    return str(a), str(r)


def test_hub_airports_filters_by_degree_and_radius(tmp_path):
    a, r = _hub_feed(tmp_path)
    # Near HUB, a tight radius: HUB qualifies (degree 3); LOW is near but below the
    # degree threshold; FAR is a hub but out of range.
    near = hub_airports([(50.0, 8.0)], 100.0, min_routes=2, airports=a, routes=r)
    assert near == {"HUB"}
    # A global radius brings FAR in too, but LOW (degree 1) still does not qualify.
    wide = hub_airports([(50.0, 8.0)], 30000.0, min_routes=2, airports=a, routes=r)
    assert wide == {"HUB", "FAR"}
    # The count cap keeps only the busiest hubs: limit=1 drops FAR (degree 2 < 3).
    top = hub_airports([(50.0, 8.0)], 30000.0, min_routes=2, limit=1,
                       airports=a, routes=r)
    assert top == {"HUB"}


def test_hub_airports_empty_without_cached_data(tmp_path, monkeypatch):
    import travelplanner.roads as roads
    monkeypatch.setattr(roads, "cache_dir", lambda: str(tmp_path))   # no routes.dat
    assert hub_airports([(50.0, 8.0)], 100.0, download=False) == set()
