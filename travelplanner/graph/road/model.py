"""Road network topology for the CCH engine (country-scale layout).

Arc and coordinate data are stored as packed `array` columns. Per-arc Validity
objects and names are interned: each arc holds a small int index into a
validity_table / name_table. At country scale most arcs share the same validity
(no seasonal restriction), so interning collapses millions of objects to a
handful of table entries, and customization evaluates each distinct validity
once per query rather than once per arc.
"""

from array import array
from collections.abc import Sequence
from dataclasses import dataclass, field

from travelplanner.graph.validity import ALWAYS, Validity


@dataclass(frozen=True)
class RoadGraph:
    # node_keys is the external identity per internal index: a list[str] for
    # arbitrary keys, or a packed array("q") for integer (OSM) ids. The reverse
    # key -> index map is built lazily (see index()) so the index-based routing
    # path never pays for it at country scale.
    node_keys: Sequence
    latitude: array
    longitude: array
    tail: array
    head: array
    base_seconds: array
    arc_validity: array          # int index into validity_table
    validity_table: tuple[Validity, ...]
    arc_name: array | None       # int index into name_table, or None
    name_table: tuple[str, ...]
    arc_class: array | None = None   # int index into class_table (OSM highway class)
    class_table: tuple[str, ...] = ()
    signal_nodes: frozenset = frozenset()   # node indices with traffic signals
    restricted_turns: frozenset = frozenset()   # forbidden (in_arc, out_arc) pairs
    _index_cache: dict | None = field(default=None, compare=False, repr=False)

    @property
    def node_count(self) -> int:
        return len(self.node_keys)

    @property
    def arc_count(self) -> int:
        return len(self.tail)

    def index(self, key) -> int:
        cache = self._index_cache
        if cache is None:
            cache = {k: i for i, k in enumerate(self.node_keys)}
            object.__setattr__(self, "_index_cache", cache)
        return cache[key]

    def key(self, index: int):
        return self.node_keys[index]

    def arcs_by_name(self, name: str) -> list[int]:
        if self.arc_name is None or name not in self.name_table:
            return []
        nidx = self.name_table.index(name)
        return [i for i, v in enumerate(self.arc_name) if v == nidx]


class RoadGraphBuilder:
    def __init__(self, store_names: bool = True) -> None:
        self._keys: list[str] = []
        self._index: dict[str, int] = {}
        # Coordinates are float32: ~0.6 m precision at these latitudes, ample for
        # nearest-node snapping, and half the memory of float64 at country scale.
        self._lat = array("f")
        self._lon = array("f")
        self._tail = array("i")
        self._head = array("i")
        # base_seconds stays 32-bit: an arc's travel time has no small bound (a
        # long ferry or coarse segment could exceed int16's ~9 h), so it is not
        # narrowed -- unlike the bounded interned indices below.
        self._secs = array("i")
        # Validity and class are indices into tiny interned tables (a handful of
        # entries each), so 16-bit indices suffice and halve those columns.
        self._arc_validity = array("h")
        self._validity_table: list[Validity] = []
        self._validity_map: dict[Validity, int] = {}
        self._store_names = store_names
        # Names intern into a large table (street names), so they keep 32-bit.
        self._arc_name = array("i") if store_names else None
        self._name_table: list[str] = []
        self._name_map: dict[str, int] = {}
        # Highway class is interned per arc (tiny table) so customization can
        # apply a per-class speed multiplier without storing strings per arc.
        self._arc_class = array("h")
        self._class_table: list[str] = []
        self._class_map: dict[str, int] = {}
        self._signal_nodes: set[int] = set()
        self._restricted_turns: frozenset = frozenset()

    def mark_signal(self, index: int) -> None:
        """Mark a node index as carrying traffic signals (for turn costs)."""
        self._signal_nodes.add(index)

    def set_restricted_turns(self, pairs) -> None:
        """Set the forbidden (in_arc, out_arc) turn pairs (from OSM restrictions)."""
        self._restricted_turns = frozenset(pairs)

    def _intern_class(self, highway: str) -> int:
        idx = self._class_map.get(highway)
        if idx is None:
            idx = len(self._class_table)
            self._class_table.append(highway)
            self._class_map[highway] = idx
        return idx

    def _intern_validity(self, validity: Validity) -> int:
        idx = self._validity_map.get(validity)
        if idx is None:
            idx = len(self._validity_table)
            self._validity_table.append(validity)
            self._validity_map[validity] = idx
        return idx

    def _intern_name(self, name: str) -> int:
        idx = self._name_map.get(name)
        if idx is None:
            idx = len(self._name_table)
            self._name_table.append(name)
            self._name_map[name] = idx
        return idx

    def add_node(self, key: str, lat: float, lon: float) -> int:
        idx = self._index.get(key)
        if idx is not None:
            return idx
        idx = len(self._keys)
        self._index[key] = idx
        self._keys.append(key)
        self._lat.append(lat)
        self._lon.append(lon)
        return idx

    def add_arc(self, from_key: str, to_key: str, seconds: float,
                validity: Validity = ALWAYS, name: str = "",
                highway: str = "") -> int:
        if from_key not in self._index:
            raise KeyError(f"Unknown from_node {from_key!r}; add_node first")
        if to_key not in self._index:
            raise KeyError(f"Unknown to_node {to_key!r}; add_node first")
        arc = len(self._tail)
        self._tail.append(self._index[from_key])
        self._head.append(self._index[to_key])
        self._secs.append(int(round(seconds)))
        self._arc_validity.append(self._intern_validity(validity))
        self._arc_class.append(self._intern_class(highway))
        if self._arc_name is not None:
            self._arc_name.append(self._intern_name(name))
        return arc

    def add_road(self, a_key: str, b_key: str, seconds: float,
                 validity: Validity = ALWAYS, name: str = "",
                 bidirectional: bool = True, highway: str = "") -> list[int]:
        arcs = [self.add_arc(a_key, b_key, seconds, validity, name, highway)]
        if bidirectional:
            arcs.append(self.add_arc(b_key, a_key, seconds, validity, name, highway))
        return arcs

    def build(self) -> RoadGraph:
        # Integer keys (OSM node ids) pack into a compact array, dropping the
        # per-node Python str/int objects; arbitrary keys stay a list.
        try:
            node_keys: Sequence = array("q", self._keys)
        except (TypeError, OverflowError):
            node_keys = list(self._keys)
        return RoadGraph(
            node_keys=node_keys,
            latitude=self._lat,
            longitude=self._lon,
            tail=self._tail,
            head=self._head,
            base_seconds=self._secs,
            arc_validity=self._arc_validity,
            validity_table=tuple(self._validity_table),
            arc_name=self._arc_name,
            name_table=tuple(self._name_table),
            arc_class=self._arc_class,
            class_table=tuple(self._class_table),
            signal_nodes=frozenset(self._signal_nodes),
            restricted_turns=self._restricted_turns,
        )
