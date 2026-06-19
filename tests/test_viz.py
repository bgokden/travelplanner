"""Route geometry export: GeoJSON + self-contained HTML map (no network)."""

from datetime import timedelta

import pytest

from travelplanner.roads import Route
from travelplanner.viz import route_map_html, save_route_map


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
