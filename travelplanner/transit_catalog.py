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
import shutil
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import timedelta

# The Mobility Database catalog export (CSV). Stable share URL maintained by
# MobilityData; columns are the SpreadsheetSchemaV2 (mdb_source_id, data_type,
# urls.latest, location.bounding_box.*, status, ...).
CATALOG_URL = "https://share.mobilitydata.org/catalogs-csv"
_CATALOG_FILE = "mobility-catalog.csv"

# The catalog changes slowly (feeds added/retired), so a week-old copy is fine;
# a GTFS feed's schedules change more often, but a week still catches most
# updates. Both are refreshed when older than this and the network is reachable;
# offline, the stale copy is kept rather than failing (see roads.refresh_if_stale).
CATALOG_MAX_AGE = timedelta(days=7)
FEED_MAX_AGE = timedelta(days=7)

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
    license_url: str = ""

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
            max_lat=max_lat, max_lon=max_lon,
            license_url=(r.get("urls.license") or "").strip())
    return out


def catalog(*, refresh: bool = False,
            max_age: timedelta | None = CATALOG_MAX_AGE) -> dict[str, Feed]:
    """All catalog feeds, downloading the CSV if missing or stale.

    The cached CSV is refreshed when older than `max_age`; `max_age=None` caches
    forever. On the age-based refresh, a failure that leaves a cached copy is
    tolerated (the copy is used with a warning) rather than failing. `refresh=True`
    forces a re-download and propagates any error -- an explicit refresh that
    cannot reach the network is a failure, not a silent fall back to stale data.
    """
    from travelplanner.roads import refresh_if_stale
    path = _catalog_path()
    if refresh:
        _download(CATALOG_URL, path)
    else:
        refresh_if_stale(path, max_age, lambda: _download(CATALOG_URL, path),
                         label="transit catalog")
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


def download_feed(feed: Feed, *,
                  max_age: timedelta | None = FEED_MAX_AGE) -> tuple[str, bool]:
    """Download a feed's GTFS zip to the shared cache; return (path, refreshed).

    `refreshed` is True when the zip was actually (re)downloaded this call, so the
    caller can re-extract. The zip is refreshed when older than `max_age`; a
    refresh that fails offline keeps the cached zip rather than failing.
    """
    from travelplanner.roads import cache_dir, refresh_if_stale
    dest = os.path.join(cache_dir(), f"feed-{feed.id}.zip")
    refreshed = refresh_if_stale(dest, max_age, lambda: _download(feed.url, dest),
                                 label=f"feed {feed.id}")
    return dest, refreshed


def _dir_with_stops(root: str) -> str | None:
    """The directory under `root` that holds stops.txt (a GTFS zip may nest its
    files in a subfolder), or None if there is none."""
    for dirpath, _dirs, files in os.walk(root):
        if "stops.txt" in files:
            return dirpath
    return None


def fetch_feed(feed: Feed, *, max_age: timedelta | None = FEED_MAX_AGE) -> str:
    """Download and extract a feed's GTFS zip; return the directory holding
    stops.txt, ready for load_timetable. Cached after the first fetch, and
    re-extracted when the zip is refreshed (older than `max_age`)."""
    from travelplanner.roads import cache_dir
    out = os.path.join(cache_dir(), f"feed-{feed.id}")
    zip_path, refreshed = download_feed(feed, max_age=max_age)
    found = _dir_with_stops(out) if os.path.isdir(out) else None
    if found is None or refreshed:
        # Never extracted, or the zip was just refreshed. Extract into a sibling
        # dir and swap atomically: a corrupt refreshed zip then cannot destroy a
        # working extract (offline-first at the extract layer too), and a feed that
        # dropped a file leaves nothing stale behind.
        tmp = out + ".new"
        if os.path.isdir(tmp):
            shutil.rmtree(tmp)
        try:
            with zipfile.ZipFile(zip_path) as z:
                z.extractall(tmp)
            if _dir_with_stops(tmp) is None:
                raise ValueError(f"feed {feed.id} archive has no stops.txt")
        except (OSError, zipfile.BadZipFile, ValueError):
            shutil.rmtree(tmp, ignore_errors=True)
            raise                      # leave the old extract intact for the caller
        if os.path.isdir(out):
            shutil.rmtree(out)
        os.replace(tmp, out)
        found = _dir_with_stops(out)
    return found
