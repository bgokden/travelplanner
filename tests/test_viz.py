"""Route geometry export: GeoJSON + self-contained HTML map (no network)."""

from datetime import timedelta

import pytest

from travelplanner.roads import Route
from travelplanner.viz import (
    itinerary_map_html,
    route_map_html,
    save_itinerary_map,
    save_route_map,
    save_segments_map,
    segments_map_html,
)
from travelplanner import plan_trip, sample_timetable, sample_trip


def _route():
    geom = ((52.44, 4.82), (52.40, 4.80), (52.31, 4.76))
    return Route(True, timedelta(minutes=22), 25.0, geometry=geom)


def test_to_geojson():
    gj = _route().to_geojson()
    assert gj["type"] == "Feature"
    assert gj["geometry"]["type"] == "LineString"
    # GeoJSON is [lon, lat] order
    assert gj["geometry"]["coordinates"][0] == [4.82, 52.44]
    assert gj["properties"]["distance_km"] == 25.0


def test_route_map_html_contains_coords():
    html = route_map_html(_route(), title="Test")
    assert "leaflet" in html.lower()
    assert "[52.44, 4.82]" in html        # lat, lon for the polyline
    assert "25.0 km" in html


def test_save_route_map(tmp_path):
    path = save_route_map(_route(), str(tmp_path / "route.html"))
    assert path.endswith("route.html")
    assert "LineString".lower() in open(path).read().lower() or "polyline" in open(path).read()


def test_not_drivable_raises():
    with pytest.raises(ValueError):
        route_map_html(Route(False))


def _itinerary():
    origin, dest, depart = sample_trip()
    return plan_trip(origin, dest, depart, sample_timetable())[0]


def test_itinerary_map_html_colors_legs_by_mode():
    html = itinerary_map_html(_itinerary(), title="Trip")
    assert "leaflet" in html.lower()
    # the dominant leg's mode colour appears (flight orange or train green)
    assert "#dd6b20" in html or "#2f855a" in html
    # legend shows leg arrows between endpoints
    assert "&rarr;" in html


def test_save_itinerary_map(tmp_path):
    path = save_itinerary_map(_itinerary(), str(tmp_path / "trip.html"))
    assert path.endswith("trip.html")
    assert "polyline" in open(path).read()


def test_itinerary_map_uses_supplied_geometry():
    """A real routed path for a leg is drawn instead of the straight endpoints."""
    it = _itinerary()
    bend = (40.0, -3.0)   # a point not on the straight line between any endpoints
    html = itinerary_map_html(it, geometries={1: [(it.legs[0].from_loc.lat,
                                                   it.legs[0].from_loc.lon),
                                                  bend,
                                                  (it.legs[0].to_loc.lat,
                                                   it.legs[0].to_loc.lon)]})
    assert "[40.0, -3.0]" in html   # the routed waypoint is in the polyline


def test_itinerary_map_ignores_too_short_geometry():
    """An empty or single-point geometry override falls back to straight endpoints
    (a 1-point drive_route path would otherwise draw nothing)."""
    it = _itinerary()
    f = it.legs[0].from_loc
    straight = itinerary_map_html(it)
    assert itinerary_map_html(it, geometries={1: []}) == straight
    assert itinerary_map_html(it, geometries={1: [(f.lat, f.lon)]}) == straight
    assert itinerary_map_html(it, geometries={99: [(0, 0), (1, 1)]}) == straight


def test_segments_map_html_draws_each_segment():
    segs = [{"coords": [[52.0, 4.0], [52.1, 4.1]], "color": "#2b6cb0", "label": "drive"},
            {"coords": [[52.1, 4.1], [48.0, 9.0]], "color": "#dd6b20", "label": "fly"}]
    html = segments_map_html(segs, title="X", header="door to door")
    assert "leaflet" in html.lower()
    assert "#dd6b20" in html and "door to door" in html
    assert "[48.0, 9.0]" in html


def test_destination_marker_uses_last_segment():
    """The Destination marker is the final segment's last point, not leg 1's end
    (a multi-leg door-to-door map must pin the true destination)."""
    segs = [{"coords": [[52.0, 4.0], [52.1, 4.1]], "color": "#2b6cb0", "label": "a"},
            {"coords": [[52.1, 4.1], [48.0, 9.0]], "color": "#dd6b20", "label": "b"}]
    html = segments_map_html(segs)
    assert "layers[layers.length-1].coords" in html   # destination from the last leg
    assert "[48.0, 9.0]" in html                       # the true endpoint


def test_segments_map_rejects_empty_coords():
    with pytest.raises(ValueError):
        segments_map_html([{"coords": [], "color": "#000", "label": "x"}])


def test_segments_map_empty_raises():
    with pytest.raises(ValueError):
        segments_map_html([])


def test_save_segments_map(tmp_path):
    segs = [{"coords": [[52.0, 4.0], [52.1, 4.1]], "color": "#2b6cb0", "label": "a"}]
    path = save_segments_map(segs, str(tmp_path / "seg.html"))
    assert path.endswith("seg.html")
    assert "polyline" in open(path).read()


def test_empty_itinerary_map_raises():
    from travelplanner.models import Itinerary
    from datetime import datetime
    with pytest.raises(ValueError):
        itinerary_map_html(Itinerary(legs=[], depart_at=datetime(2026, 7, 1), score=0.0))
