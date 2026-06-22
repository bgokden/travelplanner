"""On-disk road artifacts for offline deployment.

A region's road data is expensive to prepare: parsing the OSM extract and
computing the CCH contraction order dominate startup (minutes at country scale).
Both are deterministic and machine-independent, so a build step can produce them
once and a runtime with no network simply loads the result.

The artifact is a directory holding the RoadGraph column arrays as raw binary
(array.tofile), the contraction order as raw binary, and a small JSON sidecar
for the metadata, the interned validity table, and the name table. The node
index is rebuilt from the keys and the spatial index from the coordinates on
load (both cheap, O(n)).

    save_road_artifact(graph, order, out_dir)
    graph, order = load_road_artifact(out_dir)
"""

import json
import os
from array import array
from datetime import date

from travelplanner.graph.road.model import RoadGraph
from travelplanner.graph.validity import ServiceCalendar, Validity

FORMAT_VERSION = 4
_META = "meta.json"
_NODE_KEYS_TXT = "node_keys.txt"
_NODE_KEYS_BIN = "node_keys.bin"
_ORDER = "order.bin"
_ARC_NAME = "arc_name.bin"
_ARC_CLASS = "arc_class.bin"
_SIGNALS = "signals.bin"
_RESTRICTED = "restricted_turns.bin"
_EXP_ORDER = "expanded_order.bin"


def _calendar_to_json(cal: ServiceCalendar | None):
    if cal is None:
        return None
    return {
        "start": cal.start.isoformat(),
        "end": cal.end.isoformat(),
        "weekdays": sorted(cal.weekdays),
        "added": [d.isoformat() for d in sorted(cal.added)],
        "removed": [d.isoformat() for d in sorted(cal.removed)],
    }


def _calendar_from_json(obj):
    if obj is None:
        return None
    return ServiceCalendar(
        start=date.fromisoformat(obj["start"]),
        end=date.fromisoformat(obj["end"]),
        weekdays=frozenset(obj["weekdays"]),
        added=frozenset(date.fromisoformat(d) for d in obj["added"]),
        removed=frozenset(date.fromisoformat(d) for d in obj["removed"]),
    )


def _validity_to_json(v: Validity):
    return {
        "calendar": _calendar_to_json(v.calendar),
        "open_months": sorted(v.open_months),
        "required_conditions": sorted(v.required_conditions),
        "forbidden_conditions": sorted(v.forbidden_conditions),
    }


def _validity_from_json(obj) -> Validity:
    return Validity(
        calendar=_calendar_from_json(obj["calendar"]),
        open_months=frozenset(obj["open_months"]),
        required_conditions=frozenset(obj["required_conditions"]),
        forbidden_conditions=frozenset(obj["forbidden_conditions"]),
    )


def _write_array(path: str, typecode: str, values) -> None:
    col = values if isinstance(values, array) else array(typecode, values)
    if col.typecode != typecode:
        col = array(typecode, col)
    with open(path, "wb") as f:
        col.tofile(f)


def _read_array(path: str, typecode: str, count: int) -> array:
    col = array(typecode)
    with open(path, "rb") as f:
        col.fromfile(f, count)
    return col


def save_road_artifact(graph: RoadGraph, order, out_dir: str) -> str:
    """Write graph + contraction order to out_dir; return out_dir."""
    os.makedirs(out_dir, exist_ok=True)

    # Integer keys (a packed array) go to a binary file; arbitrary keys go to a
    # line-delimited text file (which forbids newlines in a key).
    int_keys = isinstance(graph.node_keys, array)
    has_names = graph.arc_name is not None
    has_class = graph.arc_class is not None
    meta = {
        "format_version": FORMAT_VERSION,
        "node_count": graph.node_count,
        "arc_count": graph.arc_count,
        "has_names": has_names,
        "has_class": has_class,
        "int_node_keys": int_keys,
        # Only the variable-width C types need a portability guard: array("i")
        # (C int) and array("q") (C long long) can differ across platforms. The
        # float32 "f" (IEEE-754 single, always 4 bytes) and int16 "h" (C short,
        # 2 bytes everywhere CPython runs) columns are fixed-width, so they need no
        # itemsize check. (Raw tofile is native-endian either way.)
        "itemsize_i": array("i").itemsize,
        "itemsize_q": array("q").itemsize,
        "validity_table": [_validity_to_json(v) for v in graph.validity_table],
        "name_table": list(graph.name_table),
        "class_table": list(graph.class_table),
        "signal_count": len(graph.signal_nodes),
        "restriction_count": len(graph.restricted_turns),
    }
    with open(os.path.join(out_dir, _META), "w") as f:
        json.dump(meta, f)

    if int_keys:
        _write_array(os.path.join(out_dir, _NODE_KEYS_BIN), "q", graph.node_keys)
    else:
        for key in graph.node_keys:
            if "\n" in key:
                raise ValueError(
                    f"node key {key!r} contains a newline; the artifact format "
                    "stores keys line-delimited")
        with open(os.path.join(out_dir, _NODE_KEYS_TXT), "w",
                  encoding="utf-8") as f:
            f.write("\n".join(graph.node_keys))

    _write_array(os.path.join(out_dir, "latitude.bin"), "f", graph.latitude)
    _write_array(os.path.join(out_dir, "longitude.bin"), "f", graph.longitude)
    _write_array(os.path.join(out_dir, "tail.bin"), "i", graph.tail)
    _write_array(os.path.join(out_dir, "head.bin"), "i", graph.head)
    _write_array(os.path.join(out_dir, "base_seconds.bin"), "i", graph.base_seconds)
    _write_array(os.path.join(out_dir, "arc_validity.bin"), "h", graph.arc_validity)
    if has_names:
        _write_array(os.path.join(out_dir, _ARC_NAME), "i", graph.arc_name)
    if has_class:
        _write_array(os.path.join(out_dir, _ARC_CLASS), "h", graph.arc_class)
    if graph.signal_nodes:
        _write_array(os.path.join(out_dir, _SIGNALS), "i",
                     sorted(graph.signal_nodes))
    if graph.restricted_turns:
        flat = array("i")
        for a, b in sorted(graph.restricted_turns):
            flat.append(a)
            flat.append(b)
        _write_array(os.path.join(out_dir, _RESTRICTED), "i", flat)
    _write_array(os.path.join(out_dir, _ORDER), "i", order)
    return out_dir


def save_expanded_order(order, out_dir: str) -> str:
    """Persist the turn-expanded CCH contraction order beside a base artifact."""
    os.makedirs(out_dir, exist_ok=True)
    _write_array(os.path.join(out_dir, _EXP_ORDER), "i", order)
    return out_dir


def load_expanded_order(out_dir: str) -> list[int] | None:
    """Load the turn-expanded order, or None if this artifact has none."""
    path = os.path.join(out_dir, _EXP_ORDER)
    if not os.path.exists(path):
        return None
    col = array("i")
    with open(path, "rb") as f:
        col.frombytes(f.read())
    return list(col)


def load_road_artifact(out_dir: str) -> tuple[RoadGraph, list[int]]:
    """Load a graph + contraction order written by save_road_artifact."""
    with open(os.path.join(out_dir, _META), encoding="utf-8") as f:
        meta = json.load(f)
    if meta["format_version"] != FORMAT_VERSION:
        raise ValueError(
            f"artifact format {meta['format_version']} != supported "
            f"{FORMAT_VERSION}; rebuild the region")
    if (meta["itemsize_i"] != array("i").itemsize
            or meta.get("itemsize_q", array("q").itemsize) != array("q").itemsize):
        raise ValueError(
            "artifact was built on a platform with a different int size; "
            "rebuild the region on this platform")

    node_count = meta["node_count"]
    arc_count = meta["arc_count"]
    if meta.get("int_node_keys"):
        node_keys = _read_array(os.path.join(out_dir, _NODE_KEYS_BIN), "q",
                                node_count)
    else:
        with open(os.path.join(out_dir, _NODE_KEYS_TXT), encoding="utf-8") as f:
            text = f.read()
        node_keys = text.split("\n") if node_count else []
        if len(node_keys) != node_count:
            raise ValueError(
                f"node key count {len(node_keys)} != expected {node_count}")

    latitude = _read_array(os.path.join(out_dir, "latitude.bin"), "f", node_count)
    longitude = _read_array(os.path.join(out_dir, "longitude.bin"), "f", node_count)
    tail = _read_array(os.path.join(out_dir, "tail.bin"), "i", arc_count)
    head = _read_array(os.path.join(out_dir, "head.bin"), "i", arc_count)
    base_seconds = _read_array(os.path.join(out_dir, "base_seconds.bin"), "i", arc_count)
    arc_validity = _read_array(os.path.join(out_dir, "arc_validity.bin"), "h", arc_count)
    arc_name = (_read_array(os.path.join(out_dir, _ARC_NAME), "i", arc_count)
                if meta["has_names"] else None)
    arc_class = (_read_array(os.path.join(out_dir, _ARC_CLASS), "h", arc_count)
                 if meta.get("has_class") else None)

    signal_count = meta.get("signal_count", 0)
    signal_nodes = (frozenset(_read_array(os.path.join(out_dir, _SIGNALS), "i",
                                          signal_count))
                    if signal_count else frozenset())
    restriction_count = meta.get("restriction_count", 0)
    if restriction_count:
        flat = _read_array(os.path.join(out_dir, _RESTRICTED), "i",
                           restriction_count * 2)
        restricted_turns = frozenset((flat[2 * i], flat[2 * i + 1])
                                     for i in range(restriction_count))
    else:
        restricted_turns = frozenset()

    order = list(_read_array(os.path.join(out_dir, _ORDER), "i", node_count))

    graph = RoadGraph(
        node_keys=node_keys,
        latitude=latitude,
        longitude=longitude,
        tail=tail,
        head=head,
        base_seconds=base_seconds,
        arc_validity=arc_validity,
        validity_table=tuple(_validity_from_json(v) for v in meta["validity_table"]),
        arc_name=arc_name,
        name_table=tuple(meta["name_table"]),
        arc_class=arc_class,
        class_table=tuple(meta.get("class_table", ())),
        signal_nodes=signal_nodes,
        restricted_turns=restricted_turns,
    )
    return graph, order
