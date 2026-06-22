"""Compose a default Timetable for a trip when the caller supplies none.

The transit analogue of the road layer's auto-region selection: pick the GTFS
feed(s) whose bounding box covers the trip (Mobility Database catalog), fetch and
clip them to the trip corridor, and merge with the OpenFlights flight network --
so plan_trip works end to end with nothing passed.

Network-dependent: it downloads the catalog and the selected feeds on first use,
then serves them from the shared cache. It is honest about coverage -- the
returned notes list every gap (no covering feed, a feed that failed to fetch) so
the caller can surface it rather than return a silent empty result.
"""

import zipfile
from functools import lru_cache

from travelplanner.openflights import airports_near, load_openflights
from travelplanner.transit_catalog import (
    Feed, catalog, cached_catalog, feeds_for_trip, fetch_feed)
from travelplanner.graph.scheduled import (
    Timetable, clip_timetable, fill_missing_tz, load_timetable, merge_timetables)


@lru_cache(maxsize=8)
def _load_feed(feed: Feed) -> Timetable:
    """Parse a feed's GTFS once and reuse it across trips. Parsing a national
    feed is the slow part; clipping the cached result to each corridor is cheap.
    Feed is a frozen dataclass, so it is a valid cache key."""
    return load_timetable(fetch_feed(feed))

# Padding around the origin-destination bounding box when clipping a feed to the
# corridor (~0.6 deg ~= 65 km), enough to keep access stops and a realistic route.
_CORRIDOR_MARGIN_DEG = 0.6

# Only airports within this distance of an endpoint can serve the trip; scoping
# the synthetic flight network to them keeps the scan fast (the full global
# network is ~44k synthetic flights).
_AIRPORT_RADIUS_KM = 250.0


def _corridor_bbox(points, margin: float):
    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    return (min(lats) - margin, min(lons) - margin,
            max(lats) + margin, max(lons) + margin)


def build_default_timetable(origin, dest, *, download: bool = True,
                            max_feeds: int = 1, air: bool = True,
                            ground: bool = True) -> tuple[Timetable, list[str]]:
    """Compose (Timetable, notes) for this trip with no feed supplied.

    Merges the flight network with up to `max_feeds` catalog feeds covering both
    endpoints (smallest box first), each clipped to the trip corridor so a
    national feed does not blow up the scan. `notes` records coverage gaps and any
    feed that could not be fetched, for the caller to surface.
    """
    o = (origin.lat, origin.lon)
    d = (dest.lat, dest.lon)
    parts: list[Timetable] = []
    notes: list[str] = []

    if air:
        try:
            keep = airports_near([o, d], _AIRPORT_RADIUS_KM, download=download)
            if len(keep) >= 2:
                parts.append(load_openflights(keep=keep, download=download))
            else:
                notes.append("no airports near the trip; air skipped")
        except (OSError, ValueError) as exc:
            # OSError covers urllib's URLError/HTTPError/socket timeouts on a
            # failed download, so a network blip degrades to a note, not a crash.
            notes.append(f"flight network unavailable: {exc}")

    if ground:
        try:
            cat = catalog() if download else cached_catalog()
        except (OSError, ValueError) as exc:
            cat = {}
            notes.append(f"transit catalog unavailable: {exc}")
        feeds = feeds_for_trip(o, d, catalog=cat)
        if not feeds:
            notes.append("no GTFS feed in the catalog covers this trip "
                         "(ground transit may be missing here)")
        bbox = _corridor_bbox([o, d], _CORRIDOR_MARGIN_DEG)
        for feed in feeds[:max_feeds]:
            try:
                full = _load_feed(feed)
            except (OSError, zipfile.BadZipFile, ValueError) as exc:
                notes.append(f"feed {feed.id} ({feed.name}) unavailable: {exc}")
                continue
            parts.append(clip_timetable(full, *bbox))

    # A tz-less ground feed joined to the tz-aware flight network gets each of its
    # stops the nearest located zone, instead of the table's most-common one.
    return fill_missing_tz(merge_timetables(*parts)), notes
