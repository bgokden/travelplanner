"""Geofabrik catalog parsing + cached resolution (no network)."""

import json

import pytest

from travelplanner import geofabrik, roads

_INDEX = {
    "type": "FeatureCollection",
    "features": [
        {"properties": {"id": "switzerland", "name": "Switzerland",
                        "parent": "europe",
                        "urls": {"pbf": "https://x/europe/switzerland-latest.osm.pbf"}}},
        {"properties": {"id": "bayern", "name": "Bayern", "parent": "germany",
                        "urls": {"pbf": "https://x/europe/germany/bayern-latest.osm.pbf"}}},
        {"properties": {"id": "nourl", "name": "No URL", "parent": None,
                        "urls": {}}},
    ],
}


def test_parse_catalog_skips_entries_without_pbf():
    cat = geofabrik._parse_catalog(_INDEX)
    assert set(cat) == {"switzerland", "bayern"}
    assert cat["bayern"].name == "Bayern"
    assert cat["bayern"].parent == "germany"
    assert cat["switzerland"].pbf_url.endswith("switzerland-latest.osm.pbf")


def _seed_index(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    path = geofabrik._index_path()  # honours XDG_CACHE_HOME via roads.cache_dir
    with open(path, "w", encoding="utf-8") as f:
        json.dump(_INDEX, f)
    return path


def test_cached_catalog_empty_when_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert geofabrik.cached_catalog() == {}


def test_cached_catalog_reads_index(tmp_path, monkeypatch):
    _seed_index(tmp_path, monkeypatch)
    cat = geofabrik.cached_catalog()
    assert "bayern" in cat


def test_list_regions_sorted(tmp_path, monkeypatch):
    _seed_index(tmp_path, monkeypatch)
    # list_regions downloads if absent; here the index is cached, so no network
    ids = [r.id for r in geofabrik.list_regions()]
    assert ids == ["bayern", "switzerland"]


def test_resolve_region_falls_back_to_cached_catalog(tmp_path, monkeypatch):
    _seed_index(tmp_path, monkeypatch)
    # not a curated REGION, but present in the cached catalog
    assert "bayern" not in roads.REGIONS
    assert roads.resolve_region("bayern").endswith("bayern-latest.osm.pbf")


def test_resolve_region_unknown_still_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))  # no index cached
    with pytest.raises(ValueError):
        roads.resolve_region("atlantis")


def test_geom_catalog_refresh_redownloads_each_time(tmp_path, monkeypatch):
    """refresh=True must re-download on EVERY call, not just the first (the old
    lru_cache(refresh) memoized refresh=True and returned a stale copy)."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    geofabrik._load_geom_catalog.cache_clear()
    calls = []
    geom = {"type": "FeatureCollection", "features": [
        {"type": "Feature",
         "properties": {"id": "x", "name": "X", "parent": "europe",
                        "urls": {"pbf": "https://x/x.osm.pbf"}},
         "geometry": {"type": "Polygon",
                      "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]}}]}

    def fake_download(url, dest):
        calls.append(url)
        with open(dest, "w", encoding="utf-8") as f:
            json.dump(geom, f)

    monkeypatch.setattr(geofabrik, "_download", fake_download)
    try:
        assert "x" in geofabrik._geom_catalog() and len(calls) == 1   # first build
        geofabrik._geom_catalog()
        assert len(calls) == 1                                          # cached
        geofabrik._geom_catalog(refresh=True)
        assert len(calls) == 2                                          # re-downloaded
        geofabrik._geom_catalog(refresh=True)
        assert len(calls) == 3                                          # AGAIN (the fix)
    finally:
        geofabrik._load_geom_catalog.cache_clear()
