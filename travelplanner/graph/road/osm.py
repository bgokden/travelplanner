"""Build a routable RoadGraph from an OpenStreetMap .osm.pbf via pyosmium.

Streams highway ways, turns consecutive node pairs into arcs weighted by
travel time (from maxspeed or per-highway defaults), and attaches a seasonal
Validity for ways that carry a recognized conditional winter closure.

Limitations (Phase 1): turn restrictions and barriers are ignored; the
conditional parser handles the common `motor_vehicle:conditional = no @ (Mon-Mon)`
seasonal pattern, not the full OSM conditional grammar.
"""

import re

from travelplanner.geo import bearing, haversine, turn_angle
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


def _classify(value: str) -> tuple[str, str]:
    """Split a restriction value into (kind, maneuver).

    kind is "only" for only_* relations, else "no". maneuver names the physical
    turn ("u", "left", "right", "straight"), or "any" for no_entry/no_exit (which
    carry several from/to members and forbid every such movement).
    """
    kind = "only" if value.startswith("only") else "no"
    if "u_turn" in value:
        maneuver = "u"
    elif "left" in value:
        maneuver = "left"
    elif "right" in value:
        maneuver = "right"
    elif "straight" in value:
        maneuver = "straight"
    else:
        maneuver = "any"
    return kind, maneuver


def _maneuver_pairs(from_arcs, to_arcs, maneuver, arc_bearing) -> set:
    """The (in_arc, out_arc) pairs that realize the named maneuver.

    With a single candidate the relation identifies the turn unambiguously, so
    that pair is used as-is. With several candidates -- a bidirectional way, or
    from_way == to_way at the via, contributing arcs in both directions -- the
    geometry (turn angle from the in-arc bearing to the out-arc bearing) selects
    which physical turn the relation means, so legal straight-through and
    opposite-approach movements are not over-forbidden.
    """
    pairs = [(i, o, turn_angle(arc_bearing[i], arc_bearing[o]))
             for i in from_arcs for o in to_arcs]
    if not pairs:
        return set()
    if len(pairs) == 1:
        i, o, _ = pairs[0]
        return {(i, o)}
    if maneuver == "u":
        return {(i, o) for i, o, a in pairs if abs(a) >= 150.0}
    if maneuver == "straight":
        i, o, _ = min(pairs, key=lambda p: abs(p[2]))
        return {(i, o)}
    if maneuver == "left":
        lefts = [p for p in pairs if p[2] < 0.0]
        if not lefts:
            return set()
        i, o, _ = min(lefts, key=lambda p: p[2])     # most-negative = sharpest left
        return {(i, o)}
    if maneuver == "right":
        rights = [p for p in pairs if p[2] > 0.0]
        if not rights:
            return set()
        i, o, _ = max(rights, key=lambda p: p[2])     # most-positive = sharpest right
        return {(i, o)}
    return {(i, o) for i, o, _ in pairs}              # "any": forbid every movement


def resolve_restrictions(restrictions, arc_into, arc_outof, out_by_node,
                         node_index, arc_bearing) -> set:
    """Map via-node restrictions to forbidden (in_arc, out_arc) turn pairs.

    restrictions: list of (from_ways, via_osm_node, to_ways, value) where
    from_ways/to_ways are tuples of OSM way ids (no_entry/no_exit legally carry
    several) and value is the raw OSM restriction string (e.g. "no_left_turn").
    arc_into/arc_outof: {(way, via_osm_node): [arc indices]} entering / leaving
    the via node on that way. out_by_node: {node_index: [out-arc indices]}.
    node_index: {osm_node_id: internal index}. arc_bearing: {arc index: compass
    bearing} for the arcs above, used to disambiguate the turn when a way is
    bidirectional.
    """
    forbidden: set = set()
    for from_ways, via, to_ways, value in restrictions:
        from_arcs = [a for fw in from_ways for a in arc_into.get((fw, via), ())]
        to_arcs = [a for tw in to_ways for a in arc_outof.get((tw, via), ())]
        if not from_arcs:
            continue
        kind, maneuver = _classify(value)
        if kind == "no":
            if not to_arcs:
                continue
            forbidden |= _maneuver_pairs(from_arcs, to_arcs, maneuver, arc_bearing)
            continue
        # only_: from the matching approach, every turn but the allowed one is banned.
        vidx = node_index.get(via)
        if vidx is None:
            continue
        out_arcs_at_via = out_by_node.get(vidx, ())
        if to_arcs:
            allowed = _maneuver_pairs(from_arcs, to_arcs, maneuver, arc_bearing)
            approaches = {i for i, _ in allowed}
        else:
            # only_X but the to-way is absent from the graph: the single allowed
            # turn is unavailable, so every turn from each approach is forbidden.
            allowed = set()
            approaches = set(from_arcs)
        for fa in approaches:
            for out_arc in out_arcs_at_via:
                if (fa, out_arc) not in allowed:
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
    """First pass: via-node turn restrictions as (from_ways, via, to_ways, value).

    from_ways/to_ways are tuples: no_entry restrictions legally carry several
    'from' members (no approach may enter the to-way) and no_exit several 'to'
    members, so every member is kept rather than only the last.
    """
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
            if value not in _RESTRICT_NO and value not in _RESTRICT_ONLY:
                return
            from_ways: list = []
            to_ways: list = []
            via = None
            for m in r.members:
                if m.type == "w" and m.role == "from":
                    from_ways.append(m.ref)
                elif m.type == "w" and m.role == "to":
                    to_ways.append(m.ref)
                elif m.role == "via":
                    via = m.ref if m.type == "n" else "way"   # via-way: skip
            if from_ways and to_ways and isinstance(via, int):
                self.restrictions.append(
                    (tuple(from_ways), via, tuple(to_ways), value))

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
    needed_ways = {w for fws, _, tws, _ in restr for w in (*fws, *tws)}
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
        # out-arcs are only needed at restriction via-nodes (for only_* turns).
        via_indices = {builder._index[v] for v in needed_via
                       if v in builder._index}
        out_by_node: dict = {}
        tails = builder._tail
        for i in range(len(tails)):
            t = tails[i]
            if t in via_indices:
                out_by_node.setdefault(t, []).append(i)
        # Bearings disambiguate which physical turn a restriction means; compute
        # them only for the arcs the restrictions actually reference.
        needed_arcs: set = set()
        for arcs in arc_into.values():
            needed_arcs.update(arcs)
        for arcs in arc_outof.values():
            needed_arcs.update(arcs)
        for arcs in out_by_node.values():
            needed_arcs.update(arcs)
        lat, lon = builder._lat, builder._lon
        heads = builder._head
        arc_bearing = {a: bearing(lat[tails[a]], lon[tails[a]],
                                  lat[heads[a]], lon[heads[a]])
                       for a in needed_arcs}
        forbidden = resolve_restrictions(restr, arc_into, arc_outof,
                                         out_by_node, builder._index, arc_bearing)
        builder.set_restricted_turns(forbidden)
    return builder.build()
