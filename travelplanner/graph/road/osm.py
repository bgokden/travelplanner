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

# Node highway tags that add a turn delay at the junction.
SIGNAL_TAGS = frozenset({"traffic_signals"})

# Turn-restriction relation values we honour (via-node only; via-way deferred).
_RESTRICT_NO = frozenset({
    "no_left_turn", "no_right_turn", "no_u_turn", "no_straight_on",
    "no_entry", "no_exit"})
_RESTRICT_ONLY = frozenset({
    "only_left_turn", "only_right_turn", "only_straight_on", "only_u_turn"})


def resolve_restrictions(restrictions, arc_into, arc_outof, out_by_node,
                         node_index) -> set:
    """Map via-node restrictions to forbidden (in_arc, out_arc) turn pairs.

    restrictions: list of (from_way, via_osm_node, to_way, kind) with kind in
    {"no", "only"}. arc_into/arc_outof: {(way, via_osm_node): [arc indices]}
    entering / leaving the via node on that way. out_by_node: {node_index:
    [out-arc indices]}. node_index: {osm_node_id: internal index}.
    """
    forbidden: set = set()
    for from_way, via, to_way, kind in restrictions:
        from_arcs = arc_into.get((from_way, via))
        to_arcs = arc_outof.get((to_way, via))
        if not from_arcs or not to_arcs:
            continue
        if kind == "no":
            for fa in from_arcs:
                for ta in to_arcs:
                    forbidden.add((fa, ta))
        else:  # only_: from this approach, every turn except to_arcs is banned
            vidx = node_index.get(via)
            if vidx is None:
                continue
            allowed_out = set(to_arcs)
            for fa in from_arcs:
                for out_arc in out_by_node.get(vidx, ()):
                    if out_arc not in allowed_out:
                        forbidden.add((fa, out_arc))
    return forbidden

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


def _collect_restrictions(pbf_path: str) -> list:
    """First pass: via-node turn restrictions as (from_way, via, to_way, kind)."""
    import osmium

    class _RelHandler(osmium.SimpleHandler):
        def __init__(self) -> None:
            super().__init__()
            self.restrictions: list = []

        def relation(self, r) -> None:
            tags = {t.k: t.v for t in r.tags}
            if tags.get("type") != "restriction":
                return
            value = tags.get("restriction")        # ignore restriction:conditional
            if value in _RESTRICT_NO:
                kind = "no"
            elif value in _RESTRICT_ONLY:
                kind = "only"
            else:
                return
            from_way = to_way = via = None
            for m in r.members:
                if m.type == "w" and m.role == "from":
                    from_way = m.ref
                elif m.type == "w" and m.role == "to":
                    to_way = m.ref
                elif m.role == "via":
                    via = m.ref if m.type == "n" else "way"   # via-way: skip
            if from_way and to_way and isinstance(via, int):
                self.restrictions.append((from_way, via, to_way, kind))

    handler = _RelHandler()
    handler.apply_file(pbf_path)
    return handler.restrictions


def load_road_graph(pbf_path: str,
                    allowed: frozenset[str] = DRIVING_HIGHWAYS,
                    store_names: bool = True,
                    turn_data: bool = False) -> RoadGraph:
    """Load a routable RoadGraph from an OSM extract.

    turn_data=True also collects traffic-signal nodes and parses turn-restriction
    relations (for turn-aware routing). It costs an extra file pass, so the
    node-based default leaves it off.
    """
    import osmium

    builder = RoadGraphBuilder(store_names=store_names)
    restr = _collect_restrictions(pbf_path) if turn_data else []
    needed_ways = {fw for fw, _, _, _ in restr} | {tw for _, _, tw, _ in restr}
    needed_via = {via for _, via, _, _ in restr}
    arc_into: dict = {}
    arc_outof: dict = {}

    class _Handler(osmium.SimpleHandler):
        def __init__(self) -> None:
            super().__init__()
            self.signals: set[int] = set()   # OSM ids of traffic-signal nodes

        def node(self, n) -> None:
            if not turn_data:
                return
            for t in n.tags:
                if t.k == "highway" and t.v in SIGNAL_TAGS:
                    self.signals.add(n.id)
                    break

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
            track = w.id in needed_ways

            pts = [(n.ref, n.location.lat, n.location.lon)
                   for n in w.nodes if n.location.valid()]
            for (a, alat, alon), (b, blat, blon) in zip(pts, pts[1:]):
                ka, kb = a, b  # OSM node ids are int64; keep them packed as ints
                ia = builder.add_node(ka, alat, alon)
                ib = builder.add_node(kb, blat, blon)
                if ka in self.signals:
                    builder.mark_signal(ia)
                if kb in self.signals:
                    builder.mark_signal(ib)
                dist_km = haversine(alat, alon, blat, blon)
                seconds = dist_km / speed * 3600.0
                fwd = rev = None
                if direction >= 0:
                    fwd = builder.add_arc(ka, kb, seconds, validity, name, highway)
                if direction <= 0:
                    rev = builder.add_arc(kb, ka, seconds, validity, name, highway)
                if track:
                    if kb in needed_via:
                        if fwd is not None:
                            arc_into.setdefault((w.id, kb), []).append(fwd)
                        if rev is not None:
                            arc_outof.setdefault((w.id, kb), []).append(rev)
                    if ka in needed_via:
                        if fwd is not None:
                            arc_outof.setdefault((w.id, ka), []).append(fwd)
                        if rev is not None:
                            arc_into.setdefault((w.id, ka), []).append(rev)

    handler = _Handler()
    handler.apply_file(pbf_path, locations=True)

    if restr:
        out_by_node: dict = {}
        tails = builder._tail
        for i in range(len(tails)):
            out_by_node.setdefault(tails[i], []).append(i)
        forbidden = resolve_restrictions(restr, arc_into, arc_outof,
                                         out_by_node, builder._index)
        builder.set_restricted_turns(forbidden)
    return builder.build()
