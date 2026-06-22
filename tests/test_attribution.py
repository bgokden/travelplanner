"""Attribution / licensing surface for the auto-sourced data (no network)."""

from travelplanner.attribution import (
    MOBILITY_DATABASE, OPENFLIGHTS, OPENSTREETMAP, Attribution, data_sources,
    feed_attribution, render)
from travelplanner.transit_catalog import Feed


def _feed(**kw):
    base = dict(id="9", name="Feed 9", provider="GVB", country="NL",
                url="http://x/feed.zip", min_lat=0.0, min_lon=0.0,
                max_lat=1.0, max_lon=1.0, license_url="")
    base.update(kw)
    return Feed(**base)


def test_line_is_compact_with_name_license_and_url():
    assert OPENFLIGHTS.line() == (
        "OpenFlights: Open Database License (ODbL) v1.0 "
        "<https://opendatacommons.org/licenses/odbl/1-0/>")


def test_line_omits_absent_fields():
    assert Attribution(name="Bare").line() == "Bare"
    assert Attribution(name="X", source_url="http://s").line() == "X <http://s>"


def test_feed_attribution_credits_provider_country_and_license():
    a = feed_attribution(_feed(license_url="http://x/lic"))
    assert a.name == "GVB (NL)"
    assert a.source_url == "http://x/feed.zip"
    assert a.license_url == "http://x/lic"


def test_feed_attribution_falls_back_to_name_then_id():
    assert feed_attribution(_feed(provider="")).name == "Feed 9 (NL)"
    assert feed_attribution(_feed(provider="", name="", country="")).name == "feed 9"


def test_data_sources_air_only_credits_openflights_alone():
    sources = data_sources(air=True, ground=False, road=False)
    assert sources == [OPENFLIGHTS]


def test_data_sources_ground_includes_catalog_then_each_feed():
    feeds = [_feed(id="1", provider="A"), _feed(id="2", provider="B")]
    sources = data_sources(feeds=feeds, air=False, ground=True, road=False)
    assert sources[0] is MOBILITY_DATABASE
    assert [s.name for s in sources[1:]] == ["A (NL)", "B (NL)"]


def test_data_sources_defaults_to_all_three_datasets():
    assert data_sources() == [OPENFLIGHTS, MOBILITY_DATABASE, OPENSTREETMAP]


def test_data_sources_road_only_credits_openstreetmap():
    assert data_sources(air=False, ground=False, road=True) == [OPENSTREETMAP]


def test_openstreetmap_credit_is_odbl():
    assert "ODbL" in OPENSTREETMAP.license
    assert OPENSTREETMAP.license_url == "https://opendatacommons.org/licenses/odbl/1-0/"


def test_render_marks_a_feed_with_no_listed_license():
    block = render([feed_attribution(_feed(provider="A", license_url=""))])
    assert "license: not listed in the catalog; see the feed publisher" in block


def test_render_shows_license_url_when_present():
    block = render([OPENFLIGHTS])
    assert "https://opendatacommons.org/licenses/odbl/1-0/" in block
    assert "source:  https://openflights.org/data.html" in block
