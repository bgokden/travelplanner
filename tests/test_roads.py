"""Tests for the road-data helpers that don't require network or routingkit."""

import pytest

from dataclasses import dataclass

import travelplanner.roads as roads
from travelplanner.models import Location, LocationType
from travelplanner.graph.road.model import RoadGraph, RoadGraphBuilder
from travelplanner.graph.road.spatial import NodeGrid
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


@dataclass
class _RouterShim:
    """Minimal router for _snap: a graph plus its spatial index (no routingkit)."""
    graph: RoadGraph
    node_grid: NodeGrid


def _tiny_router() -> _RouterShim:
    b = RoadGraphBuilder()
    b.add_node("a", 47.10, 9.50)
    b.add_node("b", 47.12, 9.52)
    b.add_road("a", "b", 120)
    graph = b.build()
    return _RouterShim(graph, NodeGrid.build(graph.latitude, graph.longitude))


def test_snap_within_region():
    r = _tiny_router()
    idx = _snap(r, Location("near", LocationType.HOTEL, 47.11, 9.51), "test")
    assert r.graph.key(idx) in ("a", "b")


def test_snap_out_of_region_raises():
    r = _tiny_router()
    with pytest.raises(ValueError):
        _snap(r, Location("far", LocationType.HOTEL, 0.0, 0.0), "test")


def test_snap_none_region_message_is_clear():
    # An offline data_dir load passes region=None; the error must not read
    # "None road data".
    r = _tiny_router()
    with pytest.raises(ValueError) as exc:
        _snap(r, Location("far", LocationType.HOTEL, 0.0, 0.0), None)
    msg = str(exc.value)
    assert "None road data" not in msg
    assert "the loaded road data" in msg


def test_road_router_cache_key_normalized(monkeypatch):
    # road_router(region) and road_router(region, None) must reach the cached
    # builder with identical args, so they share one cache entry (no rebuild).
    calls = []

    def fake(region, data_dir):
        calls.append((region, data_dir))
        return object()

    monkeypatch.setattr(roads, "_road_router_cached", fake)
    roads.road_router("switzerland")
    roads.road_router("switzerland", None)
    assert calls == [("switzerland", None), ("switzerland", None)]
