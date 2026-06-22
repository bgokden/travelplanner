"""Mobility Database catalog parsing and bbox-based feed selection (no network)."""

import csv
import io

from travelplanner.transit_catalog import (
    _parse_catalog, feeds_for_points, feeds_for_trip)

_COLS = [
    "mdb_source_id", "data_type", "provider", "name", "location.country_code",
    "urls.latest", "urls.direct_download", "urls.authentication_type", "status",
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
         lat0="", lat1="", lon0="", lon1=""):
    return {
        "mdb_source_id": fid, "data_type": data_type, "provider": f"P{fid}",
        "name": f"Feed {fid}", "location.country_code": "NL",
        "urls.latest": url, "urls.authentication_type": auth, "status": status,
        "location.bounding_box.minimum_latitude": lat0,
        "location.bounding_box.maximum_latitude": lat1,
        "location.bounding_box.minimum_longitude": lon0,
        "location.bounding_box.maximum_longitude": lon1,
    }


# National (covers all of NL), metro Amsterdam (small), plus rows that must be
# filtered out: inactive, key-required, GTFS-RT, and a feed with no bounding box.
CATALOG = _csv([
    _row("1", lat0="50.5", lat1="53.7", lon0="3.3", lon1="7.3"),    # national NL
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
