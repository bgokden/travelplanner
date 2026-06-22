"""Catalog of downloadable GTFS feeds (Mobility Database), selected by location.

The transit analogue of travelplanner.geofabrik: download the Mobility Database
CSV catalog once, cache it, and pick the feed(s) whose recorded bounding box
covers a trip's coordinates -- so the planner can fetch ground-transit schedules
without the user supplying a GTFS feed.

The catalog lists ~2300 GTFS schedule feeds with a bounding box and a stable
download URL each. Only openly downloadable feeds are kept: a feed that needs an
API key (authentication) or is flagged inactive/deprecated is dropped, since it
cannot be fetched unattended. Individual feeds carry their own licenses; this
module bundles no feed data -- it downloads on demand at runtime, like the road
extracts.
"""

import csv
import io
import os
import urllib.request
from dataclasses import dataclass

# The Mobility Database catalog export (CSV). Stable share URL maintained by
# MobilityData; columns are the SpreadsheetSchemaV2 (mdb_source_id, data_type,
# urls.latest, location.bounding_box.*, status, ...).
CATALOG_URL = "https://share.mobilitydata.org/catalogs-csv"
_CATALOG_FILE = "mobility-catalog.csv"

_INACTIVE = {"inactive", "deprecated"}


@dataclass(frozen=True)
class Feed:
    id: str
    name: str
    provider: str
    country: str
    url: str
    min_lat: float
    min_lon: float
    max_lat: float
    max_lon: float

    def covers(self, lat: float, lon: float) -> bool:
        return (self.min_lat <= lat <= self.max_lat
                and self.min_lon <= lon <= self.max_lon)

    @property
    def bbox_area(self) -> float:
        return (self.max_lat - self.min_lat) * (self.max_lon - self.min_lon)


def _catalog_path() -> str:
    from travelplanner.roads import cache_dir
    return os.path.join(cache_dir(), _CATALOG_FILE)


def _download(url: str, dest: str) -> None:
    tmp = dest + ".part"
    req = urllib.request.Request(url, headers={"User-Agent": "travelplanner"})
    with urllib.request.urlopen(req) as resp, open(tmp, "wb") as out:
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
    os.replace(tmp, dest)


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_catalog(text: str) -> dict[str, Feed]:
    """Parse the catalog CSV into id -> Feed, keeping only openly downloadable
    GTFS schedule feeds with a usable bounding box."""
    out: dict[str, Feed] = {}
    for r in csv.DictReader(io.StringIO(text)):
        if r.get("data_type") != "gtfs":
            continue                                   # skip GTFS-RT / GBFS
        if (r.get("status") or "").strip().lower() in _INACTIVE:
            continue
        auth = (r.get("urls.authentication_type") or "").strip()
        if auth not in ("", "0"):
            continue                                   # needs an API key: cannot auto-fetch
        url = (r.get("urls.latest") or r.get("urls.direct_download") or "").strip()
        if not url:
            continue
        coords = (_to_float(r.get("location.bounding_box.minimum_latitude")),
                  _to_float(r.get("location.bounding_box.minimum_longitude")),
                  _to_float(r.get("location.bounding_box.maximum_latitude")),
                  _to_float(r.get("location.bounding_box.maximum_longitude")))
        if any(c is None for c in coords):
            continue                                   # no bounding box: cannot place it
        fid = (r.get("mdb_source_id") or "").strip()
        if not fid:
            continue
        min_lat, min_lon, max_lat, max_lon = coords
        out[fid] = Feed(
            id=fid, name=(r.get("name") or "").strip(),
            provider=(r.get("provider") or "").strip(),
            country=(r.get("location.country_code") or "").strip(),
            url=url, min_lat=min_lat, min_lon=min_lon,
            max_lat=max_lat, max_lon=max_lon)
    return out


def catalog(*, refresh: bool = False) -> dict[str, Feed]:
    """All catalog feeds, downloading the CSV if missing or refresh is set."""
    path = _catalog_path()
    if refresh or not os.path.exists(path):
        _download(CATALOG_URL, path)
    with open(path, encoding="utf-8") as f:
        return _parse_catalog(f.read())


def cached_catalog() -> dict[str, Feed]:
    """Catalog from the cached CSV only; empty if it has not been downloaded yet
    (safe to call at runtime without touching the network)."""
    path = _catalog_path()
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return _parse_catalog(f.read())


def list_feeds(*, refresh: bool = False) -> list[Feed]:
    return sorted(catalog(refresh=refresh).values(), key=lambda fd: fd.id)


def feeds_for_points(points, *, catalog=None) -> list[Feed]:
    """Feeds whose bounding box covers EVERY point, smallest box first.

    Smallest-first puts the most specific (city/region) feed ahead of a sparse
    country-wide one; the caller fetches and merges as many as it needs.
    """
    feeds = catalog if catalog is not None else cached_catalog()
    covering = [f for f in feeds.values()
                if all(f.covers(lat, lon) for lat, lon in points)]
    return sorted(covering, key=lambda f: f.bbox_area)


def feeds_for_trip(origin, dest, *, catalog=None) -> list[Feed]:
    """Feeds covering both trip endpoints (a corridor-spanning feed)."""
    return feeds_for_points([origin, dest], catalog=catalog)
