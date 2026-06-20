"""Bundled city catalog: lookup + prefix/substring search."""

from travelplanner.catalog import lookup_city, search_cities


def test_lookup_city_known_and_unknown():
    assert lookup_city("Amsterdam") == (52.3676, 4.9041)
    assert lookup_city("amsterdam") == (52.3676, 4.9041)   # case-insensitive
    assert lookup_city("Zaandam") is None                  # not in the table


def test_search_cities_prefix_first():
    rows = search_cities("ams")
    assert rows and rows[0]["name"] == "Amsterdam"
    assert rows[0]["country"] == "Netherlands"
    assert {"name", "country", "lat", "lon"} == set(rows[0])


def test_search_cities_ranks_prefix_before_contains():
    # a query that is a prefix of one city and a substring of another
    rows = search_cities("ber")
    names = [r["name"] for r in rows]
    assert "Berlin" in names
    assert names.index("Berlin") == 0       # prefix match leads


def test_search_cities_limit_and_empty():
    assert search_cities("") == []
    assert search_cities("   ") == []
    assert len(search_cities("a", limit=3)) <= 3
    assert search_cities("zzzznotacity") == []
