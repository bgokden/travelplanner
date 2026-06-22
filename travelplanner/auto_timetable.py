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

from travelplanner.openflights import load_flight_network
from travelplanner.transit_catalog import (
    catalog, cached_catalog, feeds_for_trip, fetch_feed)
from travelplanner.graph.scheduled import (
    Timetable, clip_timetable, load_timetable, merge_timetables)

# Padding around the origin-destination bounding box when clipping a feed to the
# corridor (~0.6 deg ~= 65 km), enough to keep access stops and a realistic route.
_CORRIDOR_MARGIN_DEG = 0.6


@lru_cache(maxsize=2)
def _flight_network(download: bool) -> Timetable:
    """The OpenFlights synthetic flight network, built once per process."""
    return load_flight_network(download=download)


def _corridor_bbox(points, margin: float):
    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    return (min(lats) - margin, min(lons) - margin,
            max(lats) + margin, max(lons) + margin)


def build_default_timetable(origin, dest, *, download: bool = True,
                            max_feeds: int = 2, air: bool = True,
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
            parts.append(_flight_network(download))
        except FileNotFoundError:
            notes.append("flight network unavailable (no cached OpenFlights data; "
                         "needs one online run)")

    if ground:
        cat = catalog() if download else cached_catalog()
        feeds = feeds_for_trip(o, d, catalog=cat)
        if not feeds:
            notes.append("no GTFS feed in the catalog covers this trip "
                         "(ground transit may be missing here)")
        bbox = _corridor_bbox([o, d], _CORRIDOR_MARGIN_DEG)
        for feed in feeds[:max_feeds]:
            try:
                full = load_timetable(fetch_feed(feed))
            except (OSError, zipfile.BadZipFile, ValueError) as exc:
                notes.append(f"feed {feed.id} ({feed.name}) unavailable: {exc}")
                continue
            parts.append(clip_timetable(full, *bbox))

    return merge_timetables(*parts), notes
