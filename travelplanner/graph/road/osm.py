"""Build a routable RoadGraph from an OpenStreetMap .osm.pbf via pyosmium.

Streams highway ways, turns consecutive node pairs into arcs weighted by
travel time (from maxspeed or per-highway defaults), and attaches a seasonal
Validity for ways that carry a recognized conditional winter closure.

Limitations (Phase 1): turn restrictions and barriers are ignored; the
conditional parser handles the common `motor_vehicle:conditional = no @ (Mon-Mon)`
seasonal pattern, not the full OSM conditional grammar.
"""

import re

from travelplanner.geo import haversine
from travelplanner.graph.road.model import RoadGraph, RoadGraphBuilder
from travelplanner.graph.validity import Validity

# km/h fallbacks by highway class when maxspeed is absent/unparseable.
DEFAULT_SPEED_KMH = {
    "motorway": 110, "motorway_link": 70,
    "trunk": 90, "trunk_link": 60,
    "primary": 80, "primary_link": 50,
    "secondary": 70, "secondary_link": 50,
    "tertiary": 60, "tertiary_link": 40,
    "unclassified": 50, "residential": 30, "living_street": 10,
    "service": 20, "road": 40, "track": 20,
}
DRIVING_HIGHWAYS = frozenset(DEFAULT_SPEED_KMH)

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

_CONDITIONAL_KEYS = (
    "motor_vehicle:conditional", "motorcar:conditional",
    "vehicle:conditional", "access:conditional",
)
_CLOSURE_RE = re.compile(
    r"no\s*@\s*\(?\s*([A-Za-z]{3})\s*-\s*([A-Za-z]{3})", re.IGNORECASE)


def _month_span(first: int, last: int) -> set[int]:
    """Inclusive month set from `first` to `last`, wrapping over year-end."""
    if first <= last:
        return set(range(first, last + 1))
    return set(range(first, 13)) | set(range(1, last + 1))


def parse_seasonal_closure(tags: dict[str, str]) -> Validity:
    """Return a Validity encoding a recognized winter closure, else unrestricted.

    Handles `... :conditional = no @ (Nov-May)` by making the road open only in
    the complementary months.
    """
    for key in _CONDITIONAL_KEYS:
        value = tags.get(key)
        if not value:
            continue
        m = _CLOSURE_RE.search(value)
        if not m:
            continue
        first = _MONTHS.get(m.group(1).lower())
        last = _MONTHS.get(m.group(2).lower())
        if first is None or last is None:
            continue
        closed = _month_span(first, last)
        open_months = frozenset(set(range(1, 13)) - closed)
        if open_months:
            return Validity(open_months=open_months)
    return Validity()


def parse_maxspeed(value: str | None, fallback_kmh: float) -> float:
    if not value:
        return fallback_kmh
    value = value.strip().lower()
    mph = "mph" in value
    num = re.search(r"\d+(?:\.\d+)?", value)
    if not num:
        return fallback_kmh
    speed = float(num.group())
    return speed * 1.609344 if mph else speed


def _is_oneway(tags: dict[str, str]) -> int:
    """1 = forward only, -1 = backward only, 0 = bidirectional."""
    ow = tags.get("oneway", "").strip().lower()
    if ow in ("yes", "true", "1"):
        return 1
    if ow in ("-1", "reverse"):
        return -1
    if tags.get("junction") == "roundabout" and not ow:
        return 1
    return 0


def load_road_graph(pbf_path: str,
                    allowed: frozenset[str] = DRIVING_HIGHWAYS,
                    store_names: bool = True) -> RoadGraph:
    import osmium

    builder = RoadGraphBuilder(store_names=store_names)

    class _Handler(osmium.SimpleHandler):
        def way(self, w) -> None:
            tags = {t.k: t.v for t in w.tags}
            highway = tags.get("highway")
            if highway not in allowed:
                return
            speed = parse_maxspeed(tags.get("maxspeed"),
                                   DEFAULT_SPEED_KMH.get(highway, 40))
            validity = parse_seasonal_closure(tags)
            name = tags.get("name", "")
            direction = _is_oneway(tags)

            pts = [(n.ref, n.location.lat, n.location.lon)
                   for n in w.nodes if n.location.valid()]
            for (a, alat, alon), (b, blat, blon) in zip(pts, pts[1:]):
                ka, kb = a, b  # OSM node ids are int64; keep them packed as ints
                builder.add_node(ka, alat, alon)
                builder.add_node(kb, blat, blon)
                dist_km = haversine(alat, alon, blat, blon)
                seconds = dist_km / speed * 3600.0
                if direction >= 0:
                    builder.add_arc(ka, kb, seconds, validity, name)
                if direction <= 0:
                    builder.add_arc(kb, ka, seconds, validity, name)

    _Handler().apply_file(pbf_path, locations=True)
    return builder.build()
