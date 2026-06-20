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


def _running_server():
    server = make_server("127.0.0.1", 0)              # ephemeral port
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
