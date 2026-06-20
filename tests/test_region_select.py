"""Coordinate -> region selection: point-in-polygon, smallest, cross-border."""

import pytest

from travelplanner.geofabrik import (
    Region,
    RegionGeometry,
    _point_in_polygons,
    _smallest_containing,
)


def _square(region_id, parent, lon0, lat0, lon1, lat1):
    ring = [[lon0, lat0], [lon1, lat0], [lon1, lat1], [lon0, lat1], [lon0, lat0]]
    polygons = ([ring],)  # MultiPolygon with one polygon, one outer ring
    region = Region(region_id, region_id.title(), parent, f"https://x/{region_id}.osm.pbf")
    bbox = (lon0, lat0, lon1, lat1)
    return RegionGeometry(region, bbox, polygons)


# europe (continent, parent=None) contains a big box; "country" inside it; and a
# smaller "province" inside the country. A separate "other" country far away.
CATALOG = {
    "europe":   _square("europe", None, 0, 40, 20, 56),
    "country":  _square("country", "europe", 4, 50, 8, 54),
    "province": _square("province", "country", 4.5, 52, 5.5, 53),
    "other":    _square("other", "europe", 12, 41, 14, 43),
}


def test_point_in_polygons():
    sq = CATALOG["province"].polygons
    assert _point_in_polygons(5.0, 52.5, sq)        # inside
    assert not _point_in_polygons(9.0, 52.5, sq)    # outside (east)
    assert not _point_in_polygons(5.0, 60.0, sq)    # outside (north)


def test_smallest_containing_prefers_province():
    # a point inside province is also in country and europe; province wins
    r = _smallest_containing(CATALOG, [(52.5, 5.0)])
    assert r.id == "province"


def test_continents_excluded():
    # a point in country but outside province -> country (not europe)
    r = _smallest_containing(CATALOG, [(50.5, 7.0)])
    assert r.id == "country"


def test_trip_within_country_picks_country():
    # two points in different parts of the country (one in province, one not)
    r = _smallest_containing(CATALOG, [(52.5, 5.0), (50.5, 7.0)])
    assert r.id == "country"


def test_cross_border_returns_none():
    # one point in country, one in the far "other" country: no shared non-continent
    assert _smallest_containing(CATALOG, [(52.5, 5.0), (42.0, 13.0)]) is None


def test_region_for_helpers(monkeypatch):
    import travelplanner.geofabrik as gf
    monkeypatch.setattr(gf, "_geom_catalog", lambda refresh=False: CATALOG)
    assert gf.region_for(52.5, 5.0).id == "province"
    assert gf.region_for_trip((52.5, 5.0), (50.5, 7.0)).id == "country"
    with pytest.raises(ValueError, match="cross-border|span a border|No single"):
        gf.region_for_trip((52.5, 5.0), (42.0, 13.0))


def test_point_only_in_continent_is_not_auto_selected():
    # (lat 41, lon 1) is inside europe only (parent=None): not auto-selected, so
    # the caller raises rather than silently downloading the continent extract.
    assert _smallest_containing(CATALOG, [(41.0, 1.0)]) is None
