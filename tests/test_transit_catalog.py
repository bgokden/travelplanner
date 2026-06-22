"""Mobility Database catalog parsing and bbox-based feed selection (no network)."""

import csv
import io
import os
import zipfile
from datetime import timedelta

import pytest

from travelplanner import transit_catalog
from travelplanner.transit_catalog import (
    _dir_with_stops, _parse_catalog, feeds_for_points, feeds_for_trip)

_COLS = [
    "mdb_source_id", "data_type", "provider", "name", "location.country_code",
    "urls.latest", "urls.direct_download", "urls.authentication_type",
    "urls.license", "status",
    "location.bounding_box.minimum_latitude",
    "location.bounding_box.maximum_latitude",
    "location.bounding_box.minimum_longitude",
    "location.bounding_box.maximum_longitude",
]


def _csv(rows):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=_COLS)
    w.writeheader()
    for r in rows:
        w.writerow({c: r.get(c, "") for c in _COLS})
    return buf.getvalue()


def _row(fid, data_type="gtfs", auth="", status="", url="http://x/feed.zip",
         license_url="", lat0="", lat1="", lon0="", lon1=""):
    return {
        "mdb_source_id": fid, "data_type": data_type, "provider": f"P{fid}",
        "name": f"Feed {fid}", "location.country_code": "NL",
        "urls.latest": url, "urls.authentication_type": auth,
        "urls.license": license_url, "status": status,
        "location.bounding_box.minimum_latitude": lat0,
        "location.bounding_box.maximum_latitude": lat1,
        "location.bounding_box.minimum_longitude": lon0,
        "location.bounding_box.maximum_longitude": lon1,
    }


# National (covers all of NL), metro Amsterdam (small), plus rows that must be
# filtered out: inactive, key-required, GTFS-RT, and a feed with no bounding box.
CATALOG = _csv([
    _row("1", license_url="http://x/license-1",
         lat0="50.5", lat1="53.7", lon0="3.3", lon1="7.3"),         # national NL
    _row("2", lat0="52.3", lat1="52.43", lon0="4.7", lon1="5.0"),   # metro AMS
    _row("3", status="inactive", lat0="52.3", lat1="52.43", lon0="4.7", lon1="5.0"),
    _row("4", auth="1", lat0="52.3", lat1="52.43", lon0="4.7", lon1="5.0"),
    _row("5", data_type="gtfs-rt", lat0="52.3", lat1="52.43", lon0="4.7", lon1="5.0"),
    _row("6"),                                                       # no bounding box
])

AMS = (52.37, 4.90)
UTRECHT = (52.09, 5.12)


def test_parse_keeps_only_open_gtfs_feeds_with_bbox():
    feeds = _parse_catalog(CATALOG)
    assert set(feeds) == {"1", "2"}        # inactive/auth/rt/no-bbox dropped


def test_parse_captures_feed_license_url():
    feeds = _parse_catalog(CATALOG)
    assert feeds["1"].license_url == "http://x/license-1"
    assert feeds["2"].license_url == ""    # absent license stays empty, not None


def test_smallest_covering_feed_first():
    feeds = _parse_catalog(CATALOG)
    near = feeds_for_points([AMS], catalog=feeds)
    assert [f.id for f in near] == ["2", "1"]   # metro before national


def test_trip_needs_a_feed_covering_both_ends():
    feeds = _parse_catalog(CATALOG)
    # Only the national feed covers both Amsterdam and Utrecht.
    assert [f.id for f in feeds_for_trip(AMS, UTRECHT, catalog=feeds)] == ["1"]
    # The metro feed alone does not reach Utrecht.
    assert feeds["2"].covers(*UTRECHT) is False


def test_dir_with_stops_finds_nested_feed(tmp_path):
    sub = tmp_path / "gtfs_inner"
    sub.mkdir()
    (sub / "stops.txt").write_text("stop_id\n", encoding="utf-8")
    assert _dir_with_stops(str(tmp_path)) == str(sub)


def test_dir_with_stops_none_when_absent(tmp_path):
    (tmp_path / "readme.txt").write_text("x", encoding="utf-8")
    assert _dir_with_stops(str(tmp_path)) is None


def _age_file(path, seconds):
    """Backdate a file's mtime so it reads as `seconds` old."""
    past = os.path.getmtime(path) - seconds
    os.utime(path, (past, past))


def _gtfs_zip(path, stops_csv):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("stops.txt", stops_csv)


def test_catalog_skips_download_when_fresh(tmp_path, monkeypatch):
    path = tmp_path / "cat.csv"
    path.write_text(CATALOG, encoding="utf-8")            # fresh cached catalog
    calls = []
    monkeypatch.setattr(transit_catalog, "_catalog_path", lambda: str(path))
    monkeypatch.setattr(transit_catalog, "_download",
                        lambda url, dest: calls.append(url))
    feeds = transit_catalog.catalog()
    assert set(feeds) == {"1", "2"} and calls == []       # no network for a fresh copy


def test_catalog_refreshes_when_stale(tmp_path, monkeypatch):
    path = tmp_path / "cat.csv"
    path.write_text(CATALOG, encoding="utf-8")
    _age_file(path, timedelta(days=8).total_seconds())    # older than CATALOG_MAX_AGE
    calls = []

    def fake_dl(url, dest):
        calls.append(url)
        with open(dest, "w", encoding="utf-8") as f:
            f.write(CATALOG)

    monkeypatch.setattr(transit_catalog, "_catalog_path", lambda: str(path))
    monkeypatch.setattr(transit_catalog, "_download", fake_dl)
    transit_catalog.catalog()
    assert calls == [transit_catalog.CATALOG_URL]          # stale: refreshed


def test_catalog_offline_fallback_uses_stale_copy(tmp_path, monkeypatch):
    path = tmp_path / "cat.csv"
    path.write_text(CATALOG, encoding="utf-8")
    _age_file(path, timedelta(days=8).total_seconds())

    def boom(url, dest):
        raise OSError("offline")

    monkeypatch.setattr(transit_catalog, "_catalog_path", lambda: str(path))
    monkeypatch.setattr(transit_catalog, "_download", boom)
    with pytest.warns(UserWarning, match="refresh failed"):
        feeds = transit_catalog.catalog()
    assert set(feeds) == {"1", "2"}                        # served from the stale cache


def test_fetch_feed_reextracts_when_zip_refreshed(tmp_path, monkeypatch):
    feed = _parse_catalog(CATALOG)["1"]
    monkeypatch.setattr("travelplanner.roads.cache_dir", lambda: str(tmp_path))

    monkeypatch.setattr(transit_catalog, "_download",
                        lambda url, dest: _gtfs_zip(dest, "stop_id\nA\n"))
    d1 = transit_catalog.fetch_feed(feed)
    assert "A" in open(os.path.join(d1, "stops.txt"), encoding="utf-8").read()

    # Age the cached zip past the TTL and change what a refresh would fetch.
    zip_path = os.path.join(str(tmp_path), f"feed-{feed.id}.zip")
    _age_file(zip_path, timedelta(days=8).total_seconds())
    monkeypatch.setattr(transit_catalog, "_download",
                        lambda url, dest: _gtfs_zip(dest, "stop_id\nB\n"))
    d2 = transit_catalog.fetch_feed(feed)
    text = open(os.path.join(d2, "stops.txt"), encoding="utf-8").read()
    assert "B" in text and "A" not in text                 # re-extracted the new zip


def test_fetch_feed_corrupt_refresh_keeps_working_extract(tmp_path, monkeypatch):
    feed = _parse_catalog(CATALOG)["1"]
    monkeypatch.setattr("travelplanner.roads.cache_dir", lambda: str(tmp_path))
    monkeypatch.setattr(transit_catalog, "_download",
                        lambda url, dest: _gtfs_zip(dest, "stop_id\nA\n"))
    d1 = transit_catalog.fetch_feed(feed)
    assert "A" in open(os.path.join(d1, "stops.txt"), encoding="utf-8").read()

    # A refresh fetches a corrupt (non-zip) file. The extract must not be destroyed.
    zip_path = os.path.join(str(tmp_path), f"feed-{feed.id}.zip")
    _age_file(zip_path, timedelta(days=8).total_seconds())
    monkeypatch.setattr(transit_catalog, "_download",
                        lambda url, dest: open(dest, "wb").write(b"not a zip"))
    with pytest.raises(zipfile.BadZipFile):
        transit_catalog.fetch_feed(feed)
    assert "A" in open(os.path.join(d1, "stops.txt"), encoding="utf-8").read()
    assert not os.path.isdir(os.path.join(str(tmp_path), f"feed-{feed.id}.new"))
