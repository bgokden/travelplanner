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

from travelplanner.openflights import (
    airports_near, hub_airports, load_openflights)
from travelplanner.transit_catalog import (
    Feed, catalog, cached_catalog, feeds_for_trip, fetch_feed)
from travelplanner.graph.scheduled import (
    Timetable, clip_timetable, fill_missing_tz, link_transfer_hubs,
    load_timetable, merge_timetables)


@lru_cache(maxsize=8)
def _load_feed(feed: Feed) -> Timetable:
    """Parse a feed's GTFS once and reuse it across trips. Parsing a national
    feed is the slow part; clipping the cached result to each corridor is cheap.
    Feed is a frozen dataclass, so it is a valid cache key."""
    return load_timetable(fetch_feed(feed))

# Padding around the origin-destination bounding box when clipping a feed to the
# corridor (~0.6 deg ~= 65 km), enough to keep access stops and a realistic route.
_CORRIDOR_MARGIN_DEG = 0.6

# How many covering feeds to fetch before giving up. Bounds the cold-start cost: a
# dead catalog URL or a feed whose stops fall outside the corridor is skipped and the
# next tried, but the scan stops after this many fetches rather than downloading a
# long tail of national feeds (each parse is the slow step).
_MAX_FEED_ATTEMPTS = 4

# Stop merging feeds once the corridor-clipped trip count reaches this. Several
# covering feeds are merged (not just the smallest-box one) because a sparse
# long-distance operator can have a smaller bounding box than the dense national
# feed that actually carries the through-service -- ranking by box alone picks the
# sparse feed and misses the real train. The budget keeps the merged timetable
# plannable; the first contributing feed is always taken even if it alone exceeds it.
_MAX_MERGED_TRIPS = 80000

# Only airports within this distance of an endpoint can serve the trip; scoping
# the synthetic flight network to them keeps the scan fast (the full global
# network is ~44k synthetic flights).
_AIRPORT_RADIUS_KM = 250.0

# Hub airports (at least this many routes) within this distance of an endpoint are
# added as connection points, so a trip with no direct flight can route origin ->
# hub -> destination. Bounded to major hubs near the trip to keep the scan fast.
_HUB_RADIUS_KM = 3000.0
_HUB_MIN_ROUTES = 80


def _corridor_bbox(points, margin: float):
    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    return (min(lats) - margin, min(lons) - margin,
            max(lats) + margin, max(lons) + margin)


def build_default_timetable(origin, dest, *, download: bool = True,
                            air: bool = True,
                            ground: bool = True) -> tuple[Timetable, list[str]]:
    """Compose (Timetable, notes) for this trip with no feed supplied.

    Merges the flight network with the catalog feeds covering both endpoints
    (smallest box first), each clipped to the trip corridor, accumulating them up to
    a trip-count budget so the dense feed carrying the through-service is included
    alongside any sparse small-box one -- not just the single smallest box, which
    can be a sparse long-distance operator that misses the real train. `notes`
    records coverage gaps and any feed that could not be fetched.
    """
    o = (origin.lat, origin.lon)
    d = (dest.lat, dest.lon)
    parts: list[Timetable] = []
    notes: list[str] = []

    if air:
        try:
            near = airports_near([o, d], _AIRPORT_RADIUS_KM, download=download)
            if len(near) >= 2:
                # Add well-connected hub airports as connection points so a trip
                # with no direct flight can still route origin -> hub -> dest.
                keep = near | hub_airports([o, d], _HUB_RADIUS_KM,
                                           min_routes=_HUB_MIN_ROUTES,
                                           download=download)
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
        # Walk the covering feeds (smallest box first) and merge the ones carrying
        # corridor service, accumulating to a trip-count budget rather than stopping
        # at the single smallest box -- so a dense national feed (the one with the
        # real through-train) is included even when a sparse small-box operator ranks
        # ahead of it. A feed that fails to download (a dead catalog URL) or clips to
        # nothing is skipped and the next tried; _MAX_FEED_ATTEMPTS bounds the
        # cold-start fetch cost and the budget bounds the merged scan.
        merged_trips = attempts = 0
        for feed in feeds:
            if attempts >= _MAX_FEED_ATTEMPTS:
                break
            attempts += 1
            try:
                full = _load_feed(feed)
            except (OSError, zipfile.BadZipFile, ValueError) as exc:
                notes.append(f"feed {feed.id} ({feed.name}) unavailable: {exc}")
                continue
            clipped = clip_timetable(full, *bbox)
            if not clipped.trips:
                notes.append(f"feed {feed.id} ({feed.name}) has no service in the "
                             "trip corridor")
                continue
            # Always take the first contributing feed (merged_trips == 0); after that
            # stop once adding one would exceed the budget (keeps the merged timetable
            # plannable). merged_trips, not parts, since parts already holds flights.
            if merged_trips and merged_trips + len(clipped.trips) > _MAX_MERGED_TRIPS:
                break
            parts.append(clipped)
            merged_trips += len(clipped.trips)

    # A tz-less ground feed joined to the tz-aware flight network gets each of its
    # stops the nearest located zone, instead of the table's most-common one.
    merged = fill_missing_tz(merge_timetables(*parts))
    # Connect the flight network to co-located rail/bus stops so a trip can chain
    # ground transit with a flight (train to the airport, then fly).
    return link_transfer_hubs(merged), notes
