"""Demo service: plan_response shaping + the stdlib HTTP endpoints."""

import json
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

from travelplanner.samples import sample_timetable, sample_trip
from travelplanner.service import _parse_depart, make_server, plan_response
from datetime import datetime


def test_plan_response_shape():
    o, d, dep = sample_trip()
    resp = plan_response(o, d, dep, sample_timetable(), top_n=2)
    assert resp["origin"]["name"] and resp["dest"]["name"]
    assert resp["objective"] == "air_priority"
    assert resp["warnings"] == []
    assert resp["options"], "sample trip should yield at least one itinerary"
    opt = resp["options"][0]
    assert opt["segments"]
    seg = opt["segments"][0]
    assert {"coords", "color", "mode", "label"} <= set(seg)
    assert len(seg["coords"]) >= 2                 # at least endpoints
    assert all(len(p) == 2 for p in seg["coords"])  # [lat, lon] pairs


def test_plan_response_warns_on_empty_and_transit_fallback():
    tt = sample_timetable()
    depart = datetime(2026, 7, 1, 8, 0)
    # far-apart points the sample feed can't connect -> empty + a clear warning
    empty = plan_response("0,0", "10,80", depart, tt)
    assert empty["options"] == []
    assert empty["warnings"] and "No route" in empty["warnings"][0]
    # transit access that degrades to a car-only result is flagged
    o, d, _ = sample_trip()
    res = plan_response(o, d, depart, tt, access="transit")
    if res["options"] and not any(
            leg["mode"] in ("train", "ferry", "flight")
            for opt in res["options"] for leg in opt["legs"]):
        assert any("transit" in w for w in res["warnings"])


def test_plan_response_rejects_unknown_objective():
    o, d, dep = sample_trip()
    with pytest.raises(ValueError):
        plan_response(o, d, dep, sample_timetable(), objective="nonsense")


def test_parse_depart_formats_and_default():
    default = datetime(2026, 7, 1, 8, 0)
    assert _parse_depart("", default) is default
    assert _parse_depart(None, default) is default
    assert _parse_depart("2026-07-01T09:30", default) == datetime(2026, 7, 1, 9, 30)
    assert _parse_depart("2026-07-01 09:30", default) == datetime(2026, 7, 1, 9, 30)
    with pytest.raises(ValueError):
        _parse_depart("not-a-time", default)


def _car_itinerary():
    from travelplanner.models import (CostLevel, Itinerary, Leg, Location,
                                      LocationType, Mode)
    from datetime import timedelta
    a = Location("A", LocationType.CITY, 47.14, 9.52)
    b = Location("B", LocationType.CITY, 47.07, 9.50)
    leg = Leg(Mode.CAR, a, b, 8.0, timedelta(minutes=12), timedelta(0),
              CostLevel.MEDIUM)
    return Itinerary([leg], datetime(2026, 7, 1, 8, 0), 0.0)


def test_road_geometries_attaches_routed_path(monkeypatch):
    # _road_geometries must read the real routed path off a Route (regression:
    # it referenced a non-existent .feasible attribute instead of .drivable).
    import travelplanner.service as service
    from travelplanner.roads import Route
    from datetime import timedelta
    geom = ((47.14, 9.52), (47.10, 9.51), (47.07, 9.50))

    def fake_drive_route(origin, dest, **kwargs):
        return Route(True, timedelta(minutes=12), 8.0, geometry=geom)

    monkeypatch.setattr(service, "drive_route", fake_drive_route)
    it = _car_itinerary()
    geoms, warnings = service._road_geometries(
        it, region="liechtenstein", data_dir=None, depart_at=it.depart_at,
        turn_aware=False)
    assert warnings == []
    assert geoms[1] == [[47.14, 9.52], [47.10, 9.51], [47.07, 9.50]]


def test_road_geometries_warns_when_region_unresolvable(monkeypatch):
    import travelplanner.service as service

    def boom(origin, dest, **kwargs):
        raise ValueError("no region covers these points")

    monkeypatch.setattr(service, "drive_route", boom)
    it = _car_itinerary()
    geoms, warnings = service._road_geometries(
        it, region=None, data_dir=None, depart_at=it.depart_at, turn_aware=False)
    assert geoms == {}
    assert warnings and "no region" in warnings[0]


def test_geocode_suggestions_bundled_then_online(monkeypatch):
    import travelplanner.service as service

    class _Srv:
        online = False
        user_agent = "test"
        airports = ()
        geo_cache: dict = {}
        last_nominatim = 0.0

    srv = _Srv()
    srv.timetable = sample_timetable()
    assert service._geocode_suggestions(srv, "a", 8) == []        # too short
    ams = service._geocode_suggestions(srv, "amsterd", 8)
    assert ams and ams[0]["label"].startswith("Amsterdam")
    assert ams[0]["source"] == "city"

    # transit stops from the feed are offered (airports excluded -> handled below)
    srv.geo_cache = {}
    stn = service._geocode_suggestions(srv, "midvale", 8)
    assert stn and stn[0]["source"] == "station" and "Midvale" in stn[0]["label"]

    # airports are offered from the (injected) airport index
    srv.geo_cache = {}
    srv.airports = ({"iata": "AMS", "name": "Schiphol", "city": "Amsterdam",
                     "country": "Netherlands", "lat": 52.31, "lon": 4.76},)
    air = service._geocode_suggestions(srv, "schip", 8)
    assert air and air[0]["source"] == "airport" and "(AMS)" in air[0]["label"]

    # online: a name absent from the bundled table comes from Nominatim
    srv.online = True
    srv.geo_cache = {}
    srv.airports = ()
    monkeypatch.setattr(service, "nominatim_search",
                        lambda q, **kw: [{"name": "Zaandam, NL",
                                          "lat": 52.44, "lon": 4.83}])
    out = service._geocode_suggestions(srv, "zaandam", 8)
    assert any(s["source"] == "osm" and "Zaandam" in s["label"] for s in out)


def test_geocode_dedups_osm_by_label(monkeypatch):
    import travelplanner.service as service

    class _Srv:
        online = True
        user_agent = "test"
        airports = ()
        geo_cache: dict = {}
        last_nominatim = 0.0

    srv = _Srv()
    srv.timetable = sample_timetable()
    monkeypatch.setattr(service, "nominatim_search", lambda q, **kw: [
        {"name": "Rotterdam, NL", "lat": 51.92, "lon": 4.48},
        {"name": "Rotterdam, NL", "lat": 51.93, "lon": 4.49},   # dup label -> dropped
        {"name": "Rotterdam, NY", "lat": 42.80, "lon": -73.95}])
    osm = [s for s in service._geocode_suggestions(srv, "rotterdam", 8)
           if s["source"] == "osm"]
    assert [s["label"] for s in osm].count("Rotterdam, NL") == 1


def test_search_stops_excludes_airports():
    from travelplanner.service import _search_stops
    tt = sample_timetable()
    names = [s.name for s in _search_stops(tt, "westport", 8)]
    assert "Westport Station" in names
    assert "Westport Airport" not in names      # airports come from OpenFlights
    assert _search_stops(tt, "w", 8) == []       # too short


def test_default_timetable_falls_back_when_network_empty(monkeypatch):
    # an empty flight network (e.g. an over-tight route filter returns no hubs)
    # must fall back to the sample feed, not be served as a routeless timetable.
    import travelplanner.service as service
    from travelplanner.graph.scheduled import Timetable
    monkeypatch.setattr(service, "load_flight_network", lambda **kw: Timetable())
    tt, source = service._default_timetable(online=False)
    assert source == "sample"
    assert tt.stops


def test_default_timetable_falls_back_on_load_error(monkeypatch):
    import travelplanner.service as service

    def boom(**kw):
        raise OSError("offline, no cache")

    monkeypatch.setattr(service, "load_flight_network", boom)
    tt, source = service._default_timetable(online=True)
    assert source == "sample" and tt.stops


def test_make_server_offline_airport_download_failure_does_not_crash(monkeypatch):
    # online=True with no network must not crash make_server: the timetable falls
    # back to the sample feed AND the airport index degrades to empty (regression:
    # load_airports re-raised URLError after the timetable fallback succeeded).
    import travelplanner.service as service

    def boom(**kw):
        raise OSError("offline, no cache")

    monkeypatch.setattr(service, "load_airports", boom)
    server = service.make_server("127.0.0.1", 0, online=True,
                                 timetable=sample_timetable())
    try:
        assert server.airports == ()                          # degraded, not crashed
        assert server.example == service._SAMPLE_EXAMPLE       # sample feed example
    finally:
        server.server_close()


def _running_server():
    # pin the sample feed so the test is deterministic and never touches the
    # network or the OpenFlights cache.
    server = make_server("127.0.0.1", 0, online=False, timetable=sample_timetable())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _get(base, path):
    with urllib.request.urlopen(base + path, timeout=5) as resp:
        return resp.status, resp.read()


def test_http_endpoints_end_to_end():
    server, thread = _running_server()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"

        status, body = _get(base, "/api/health")
        assert status == 200 and json.loads(body)["status"] == "ok"

        status, body = _get(base, "/")
        html = body.decode()
        assert status == 200 and "travelplanner demo" in html
        assert "leaflet" in html.lower()

        status, body = _get(base, "/api/example")
        example = json.loads(body)
        assert {"origin", "dest", "depart"} <= set(example)
        # the offered example must actually route in the served (sample) feed
        ex_query = urllib.parse.urlencode({
            "origin": example["origin"], "dest": example["dest"],
            "depart": example["depart"], "top": "1"})
        ex_plan = json.loads(_get(base, "/api/plan?" + ex_query)[1])
        assert ex_plan["options"], "the demo's own example must produce a route"

        # autocomplete (offline server -> bundled cities; sample feed stations)
        status, body = _get(base, "/api/geocode?q=amsterd")
        sugg = json.loads(body)["suggestions"]
        assert status == 200 and any("Amsterdam" in s["label"] for s in sugg)
        assert json.loads(_get(base, "/api/geocode?q=a")[1])["suggestions"] == []
        # transit stops from the bundled sample feed surface as STATION
        midvale = json.loads(_get(base, "/api/geocode?q=midvale")[1])["suggestions"]
        assert any(s["source"] == "station" and "Midvale" in s["label"]
                   for s in midvale)

        # plan over the pinned sample feed (its known-good trip), not the example
        query = urllib.parse.urlencode({
            "origin": "47.0,7.005", "dest": "45.0,9.01",
            "depart": "2026-07-01T08:00", "top": "2"})
        status, body = _get(base, "/api/plan?" + query)
        data = json.loads(body)
        assert status == 200 and data["options"]
        assert data["options"][0]["segments"]

        # missing dest -> 400 with an error message
        with pytest.raises(urllib.error.HTTPError) as exc:
            _get(base, "/api/plan?origin=x")
        assert exc.value.code == 400
        assert "error" in json.loads(exc.value.read())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
