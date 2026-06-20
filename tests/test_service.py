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
    assert service._geocode_suggestions(srv, "a", 8) == []        # too short
    ams = service._geocode_suggestions(srv, "amsterd", 8)
    assert ams and ams[0]["label"].startswith("Amsterdam")
    assert ams[0]["source"] == "city"

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


def _running_server():
    server = make_server("127.0.0.1", 0, online=False)   # offline: deterministic
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

        # autocomplete (offline server -> bundled cities)
        status, body = _get(base, "/api/geocode?q=amsterd")
        sugg = json.loads(body)["suggestions"]
        assert status == 200 and any("Amsterdam" in s["label"] for s in sugg)
        assert json.loads(_get(base, "/api/geocode?q=a")[1])["suggestions"] == []

        query = urllib.parse.urlencode({
            "origin": example["origin"], "dest": example["dest"],
            "depart": example["depart"], "top": "2"})
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
