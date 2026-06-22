"""Offline road artifact: save -> load must reproduce the graph exactly and,
with the persisted contraction order, route identically."""

from datetime import date

import pytest

from travelplanner.graph.validity import ServiceCalendar, Validity
from travelplanner.graph.road.model import RoadGraphBuilder
from travelplanner.graph.road.store import (
    load_road_artifact,
    save_road_artifact,
)
from travelplanner.graph.road import CCHRoadRouter

SUMMER = Validity(calendar=ServiceCalendar(start=date(2026, 5, 1),
                                           end=date(2026, 10, 31)),
                  open_months=frozenset({6, 7, 8}),
                  forbidden_conditions=frozenset({"flood"}))


def _graph(store_names: bool):
    b = RoadGraphBuilder(store_names=store_names)
    coords = {"a": (47.10, 9.50), "b": (47.12, 9.52), "c": (47.15, 9.55),
              "d": (47.05, 9.48)}
    for key, (lat, lon) in coords.items():
        b.add_node(key, lat, lon)
    b.add_road("a", "b", 120, name="main")
    b.add_road("b", "c", 200, validity=SUMMER, name="pass")
    b.add_road("a", "d", 300, name="loop")
    b.add_road("d", "c", 250, name="loop")
    return b.build()


def _assert_same_graph(g, h):
    assert list(h.node_keys) == list(g.node_keys)
    # the reverse map is lazy now; it must still resolve every key correctly
    for i, k in enumerate(g.node_keys):
        assert h.index(k) == i
    assert list(h.latitude) == list(g.latitude)
    assert list(h.longitude) == list(g.longitude)
    assert list(h.tail) == list(g.tail)
    assert list(h.head) == list(g.head)
    assert list(h.base_seconds) == list(g.base_seconds)
    assert list(h.arc_validity) == list(g.arc_validity)
    assert h.validity_table == g.validity_table
    assert h.name_table == g.name_table
    if g.arc_name is None:
        assert h.arc_name is None
    else:
        assert list(h.arc_name) == list(g.arc_name)


def test_roundtrip_preserves_graph(tmp_path):
    g = _graph(store_names=True)
    save_road_artifact(g, CCHRoadRouter(g).order, str(tmp_path))
    h, order = load_road_artifact(str(tmp_path))
    _assert_same_graph(g, h)
    assert len(order) == g.node_count


def test_roundtrip_without_names(tmp_path):
    g = _graph(store_names=False)
    assert g.arc_name is None
    save_road_artifact(g, CCHRoadRouter(g).order, str(tmp_path))
    h, _ = load_road_artifact(str(tmp_path))
    _assert_same_graph(g, h)


def test_loaded_order_routes_identically(tmp_path):
    g = _graph(store_names=True)
    fresh = CCHRoadRouter(g)
    save_road_artifact(g, fresh.order, str(tmp_path))
    h, order = load_road_artifact(str(tmp_path))
    loaded = CCHRoadRouter(h, order=order)

    day = date(2026, 7, 1)  # summer: the seasonal arc is open
    for src in g.node_keys:
        for dst in g.node_keys:
            a = fresh.customize(day).route(src, dst)
            b = loaded.customize(day).route(src, dst)
            assert (a is None) == (b is None)
            if a is not None:
                assert a.seconds == b.seconds


def test_seasonal_validity_survives_roundtrip(tmp_path):
    g = _graph(store_names=True)
    save_road_artifact(g, CCHRoadRouter(g).order, str(tmp_path))
    h, order = load_road_artifact(str(tmp_path))
    router = CCHRoadRouter(h, order=order)

    # Winter closes the seasonal "pass" arc (open_months {6,7,8}); a->c must
    # detour via the loop, so the time differs from the summer direct route.
    summer = router.customize(date(2026, 7, 1)).route("a", "c")
    winter = router.customize(date(2026, 1, 1)).route("a", "c")
    assert summer is not None and winter is not None
    assert winter.seconds > summer.seconds


def test_newline_in_key_rejected(tmp_path):
    b = RoadGraphBuilder()
    b.add_node("a\nb", 47.0, 9.0)
    with pytest.raises(ValueError):
        save_road_artifact(b.build(), [0], str(tmp_path))


def _int_graph():
    b = RoadGraphBuilder(store_names=False)
    ids = [100200300400, 100200300401, 100200300402]
    for k, lon in zip(ids, (9.50, 9.52, 9.55)):
        b.add_node(k, 47.10, lon)
    b.add_road(ids[0], ids[1], 120)
    b.add_road(ids[1], ids[2], 200)
    return b.build(), ids


def test_integer_keys_pack_into_array():
    from array import array
    g, ids = _int_graph()
    assert isinstance(g.node_keys, array)
    assert g.node_keys.typecode == "q"
    assert list(g.node_keys) == ids
    assert g.key(0) == ids[0]
    assert g.index(ids[2]) == 2


def test_integer_keys_roundtrip_binary(tmp_path):
    from array import array
    g, ids = _int_graph()
    save_road_artifact(g, CCHRoadRouter(g).order, str(tmp_path))
    assert (tmp_path / "node_keys.bin").exists()
    assert not (tmp_path / "node_keys.txt").exists()
    h, _ = load_road_artifact(str(tmp_path))
    assert isinstance(h.node_keys, array)
    assert list(h.node_keys) == ids
    for i, k in enumerate(ids):
        assert h.index(k) == i


def test_arc_class_roundtrip(tmp_path):
    b = RoadGraphBuilder(store_names=False)
    for k, lon in [(1, 9.50), (2, 9.52), (3, 9.55)]:
        b.add_node(k, 47.10, lon)
    b.add_road(1, 2, 100, highway="motorway")
    b.add_road(2, 3, 100, highway="residential")
    g = b.build()
    assert "motorway" in g.class_table and "residential" in g.class_table
    save_road_artifact(g, CCHRoadRouter(g).order, str(tmp_path))
    h, _ = load_road_artifact(str(tmp_path))
    assert h.class_table == g.class_table
    assert list(h.arc_class) == list(g.arc_class)


def test_string_keys_stay_text(tmp_path):
    g = _graph(store_names=True)
    assert isinstance(g.node_keys, list)
    save_road_artifact(g, CCHRoadRouter(g).order, str(tmp_path))
    assert (tmp_path / "node_keys.txt").exists()
    assert not (tmp_path / "node_keys.bin").exists()


def test_graph_columns_use_compact_dtypes():
    """Coordinates are float32 and the interned index columns are 16-bit, halving
    those columns at country scale; a future widening would silently undo it."""
    g = _graph(store_names=True)
    assert g.latitude.typecode == "f" and g.longitude.typecode == "f"
    assert g.arc_validity.typecode == "h"
    assert g.arc_class.typecode == "h"
    assert g.arc_name.typecode == "i"            # street-name table can exceed 16-bit
    assert g.tail.typecode == "i" and g.head.typecode == "i"   # node indices need 32-bit


def test_roundtrip_preserves_compact_dtypes(tmp_path):
    """The artifact must store and reload the narrow dtypes, not re-widen them."""
    g = _graph(store_names=True)
    save_road_artifact(g, CCHRoadRouter(g).order, str(tmp_path))
    h, _ = load_road_artifact(str(tmp_path))
    assert h.latitude.typecode == "f" and h.longitude.typecode == "f"
    assert h.arc_validity.typecode == "h" and h.arc_class.typecode == "h"


def test_old_format_version_rejected(tmp_path):
    """A stale-format artifact must fail loudly (rebuild), not misread its bytes
    under the new dtypes."""
    import json
    g = _graph(store_names=True)
    save_road_artifact(g, CCHRoadRouter(g).order, str(tmp_path))
    meta_path = tmp_path / "meta.json"
    meta = json.loads(meta_path.read_text())
    meta["format_version"] = 3                   # a pre-compaction artifact
    meta_path.write_text(json.dumps(meta))
    with pytest.raises(ValueError, match="rebuild"):
        load_road_artifact(str(tmp_path))
