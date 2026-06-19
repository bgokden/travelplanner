"""Tests for the road-data helpers that don't require network or routingkit."""

import pytest

from travelplanner.models import Location, LocationType
from travelplanner.graph.road.model import RoadGraphBuilder
from travelplanner.roads import _coerce, _snap, resolve_region


def test_resolve_region_known_name():
    assert resolve_region("switzerland").endswith("switzerland-latest.osm.pbf")


def test_resolve_region_url_and_path_passthrough():
    url = "https://example.com/x.osm.pbf"
    assert resolve_region(url) == url
    assert resolve_region("/data/region.osm.pbf") == "/data/region.osm.pbf"


def test_resolve_region_unknown_raises():
    with pytest.raises(ValueError):
        resolve_region("atlantis")


def test_coerce_latlon_string():
    loc = _coerce("47.1,9.5")
    assert (round(loc.lat, 1), round(loc.lon, 1)) == (47.1, 9.5)


def test_coerce_city_name():
    loc = _coerce("London")
    assert loc.type is LocationType.CITY
    assert 51 < loc.lat < 52


def test_coerce_location_passthrough():
    loc = Location("X", LocationType.HOTEL, 1.0, 2.0)
    assert _coerce(loc) is loc


def _tiny_graph():
    b = RoadGraphBuilder()
    b.add_node("a", 47.10, 9.50)
    b.add_node("b", 47.12, 9.52)
    b.add_road("a", "b", 120)
    return b.build()


def test_snap_within_region():
    g = _tiny_graph()
    key = _snap(g, Location("near", LocationType.HOTEL, 47.11, 9.51), "test")
    assert key in ("a", "b")


def test_snap_out_of_region_raises():
    g = _tiny_graph()
    with pytest.raises(ValueError):
        _snap(g, Location("far", LocationType.HOTEL, 0.0, 0.0), "test")
