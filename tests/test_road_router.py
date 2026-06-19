"""Phase 1 road engine tests on a controlled Swiss alpine network.

Corridor A (Grimsel pass, seasonal): Interlaken-Innertkirchen-Gletsch-Brig.
Corridor B (Loetschberg tunnel, always open): Interlaken-Spiez-Kandersteg-Visp-Brig.
A is faster (5100s) but closes in winter; B is slower (6000s) but always open.
"""

from datetime import date

import pytest

from travelplanner.graph.validity import ServiceCalendar, Validity

routingkit = pytest.importorskip("routingkit_cch")

from travelplanner.graph.road import CCHRoadRouter, RoadGraphBuilder  # noqa: E402

SUMMER_PASS = ServiceCalendar(start=date(2026, 5, 1), end=date(2026, 10, 31))

COORDS = {
    "interlaken": (46.69, 7.86),
    "innertkirchen": (46.70, 8.24),
    "gletsch": (46.56, 8.36),
    "brig": (46.32, 7.99),
    "spiez": (46.69, 7.68),
    "kandersteg": (46.50, 7.67),
    "visp": (46.29, 7.88),
    "bern": (46.95, 7.44),
}


def _swiss_graph(pass_validity: Validity):
    b = RoadGraphBuilder()
    for key, (lat, lon) in COORDS.items():
        b.add_node(key, lat, lon)
    # Corridor A: Grimsel pass (the middle segment is seasonal).
    b.add_road("interlaken", "innertkirchen", 1500, name="A1")
    b.add_road("innertkirchen", "gletsch", 1800, validity=pass_validity,
               name="Grimsel")
    b.add_road("gletsch", "brig", 1800, name="A3")
    # Corridor B: Loetschberg, always open.
    b.add_road("interlaken", "spiez", 1200, name="B1")
    b.add_road("spiez", "kandersteg", 1500, name="B2")
    b.add_road("kandersteg", "visp", 2400, name="Loetschberg")
    b.add_road("visp", "brig", 900, name="B4")
    # Extra connectivity (must not create a shorter path).
    b.add_road("bern", "interlaken", 1500, name="C1")
    b.add_road("bern", "spiez", 1200, name="C2")
    return b.build()


def test_summer_routes_over_the_pass():
    router = CCHRoadRouter(_swiss_graph(Validity(calendar=SUMMER_PASS)))
    road = router.customize(date(2026, 7, 15))
    path = road.route("interlaken", "brig")
    assert path is not None
    assert path.seconds == 5100
    assert "gletsch" in path.node_keys      # took the pass
    assert "kandersteg" not in path.node_keys


def test_winter_reroutes_through_tunnel():
    router = CCHRoadRouter(_swiss_graph(Validity(calendar=SUMMER_PASS)))
    road = router.customize(date(2026, 1, 15))
    path = road.route("interlaken", "brig")
    assert path is not None
    assert path.seconds == 6000
    assert "kandersteg" in path.node_keys    # forced onto the tunnel
    assert "gletsch" not in path.node_keys


def test_condition_flag_closes_pass():
    # Pass requires an explicit "pass_open" condition flag.
    graph = _swiss_graph(Validity(required_conditions=frozenset({"pass_open"})))
    router = CCHRoadRouter(graph)
    open_road = router.customize(date(2026, 7, 15), frozenset({"pass_open"}))
    assert open_road.route("interlaken", "brig").seconds == 5100
    closed_road = router.customize(date(2026, 7, 15), frozenset())
    assert closed_road.route("interlaken", "brig").seconds == 6000


def test_partial_update_closes_and_reopens_pass():
    router = CCHRoadRouter(_swiss_graph(Validity(calendar=SUMMER_PASS)))
    road = router.customize(date(2026, 7, 15))
    assert road.route("interlaken", "brig").seconds == 5100   # pass open

    road.close_named("Grimsel")
    rerouted = road.route("interlaken", "brig")
    assert rerouted.seconds == 6000
    assert "kandersteg" in rerouted.node_keys

    road.open_named("Grimsel")
    assert road.route("interlaken", "brig").seconds == 5100   # pass back


def test_customized_caches_and_reuses_metric():
    router = CCHRoadRouter(_swiss_graph(Validity(calendar=SUMMER_PASS)))
    a = router.customized(date(2026, 7, 15))
    b = router.customized(date(2026, 7, 15))
    assert a is b                                  # same (day, conditions) reused
    c = router.customized(date(2026, 1, 15))
    assert c is not a                              # different key -> different metric
    assert a.route("interlaken", "brig").seconds == 5100
    assert c.route("interlaken", "brig").seconds == 6000


def test_customize_returns_fresh_independent_metric():
    router = CCHRoadRouter(_swiss_graph(Validity(calendar=SUMMER_PASS)))
    one = router.customize(date(2026, 7, 15))
    two = router.customize(date(2026, 7, 15))
    assert one is not two                          # customize() never caches
    one.close_named("Grimsel")                     # mutating one must not affect two
    assert one.route("interlaken", "brig").seconds == 6000
    assert two.route("interlaken", "brig").seconds == 5100


def test_validity_is_interned():
    # Many arcs share ALWAYS plus one seasonal pass -> tiny validity table.
    graph = _swiss_graph(Validity(calendar=SUMMER_PASS))
    assert len(graph.validity_table) == 2          # ALWAYS + the pass validity
    assert graph.arc_count > len(graph.validity_table)
    # Every arc references a valid table entry.
    assert all(0 <= i < len(graph.validity_table) for i in graph.arc_validity)
    # Interned names still resolve for partial updates.
    assert graph.arcs_by_name("Grimsel")


def test_unreachable_returns_none():
    b = RoadGraphBuilder()
    b.add_node("x", 46.0, 7.0)
    b.add_node("y", 47.0, 8.0)   # no arcs between components
    b.add_node("z", 47.1, 8.1)
    b.add_road("y", "z", 600)
    router = CCHRoadRouter(b.build())
    road = router.customize(date(2026, 7, 1))
    assert road.route("x", "y") is None
    assert road.route("y", "z").seconds == 600
