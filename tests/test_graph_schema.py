from datetime import date, datetime, timedelta

import pytest

from travelplanner.models import Mode
from travelplanner.graph.schema import (
    Connection,
    Edge,
    MultimodalGraph,
    Node,
    NodeType,
)
from travelplanner.graph.validity import ServiceCalendar, Validity

SUMMER = ServiceCalendar(start=date(2026, 6, 1), end=date(2026, 9, 30))


def _place(node_id: str) -> Node:
    return Node(id=node_id, type=NodeType.PLACE, lat=0.0, lon=0.0, name=node_id)


def test_edge_requires_exactly_one_traversal_kind():
    with pytest.raises(ValueError):
        Edge(id="e", from_node="a", to_node="b", mode=Mode.CAR)  # neither
    with pytest.raises(ValueError):
        Edge(id="e", from_node="a", to_node="b", mode=Mode.TRAIN,
             static_seconds=60.0,
             connections=(Connection(datetime(2026, 7, 1, 9),
                                      datetime(2026, 7, 1, 10)),))  # both


def test_static_edge_earliest_arrival():
    e = Edge(id="e", from_node="a", to_node="b", mode=Mode.CAR,
             static_seconds=1800.0)
    t = datetime(2026, 7, 1, 9, 0)
    assert e.earliest_arrival(t) == t + timedelta(minutes=30)


def test_scheduled_edge_picks_earliest_future_connection():
    conns = (
        Connection(datetime(2026, 7, 1, 8, 0), datetime(2026, 7, 1, 9, 0)),
        Connection(datetime(2026, 7, 1, 10, 0), datetime(2026, 7, 1, 11, 0)),
        Connection(datetime(2026, 7, 1, 12, 0), datetime(2026, 7, 1, 13, 0)),
    )
    e = Edge(id="e", from_node="a", to_node="b", mode=Mode.TRAIN,
             connections=conns)
    # Departing 9:30 -> next train is the 10:00, arriving 11:00.
    assert e.earliest_arrival(datetime(2026, 7, 1, 9, 30)) == \
        datetime(2026, 7, 1, 11, 0)


def test_scheduled_edge_no_future_connection():
    conns = (Connection(datetime(2026, 7, 1, 8, 0),
                        datetime(2026, 7, 1, 9, 0)),)
    e = Edge(id="e", from_node="a", to_node="b", mode=Mode.TRAIN,
             connections=conns)
    assert e.earliest_arrival(datetime(2026, 7, 1, 9, 30)) is None


def test_seasonal_edge_inactive_returns_none():
    e = Edge(id="ferry", from_node="a", to_node="b", mode=Mode.FERRY,
             static_seconds=3600.0, validity=Validity(calendar=SUMMER))
    # In season.
    assert e.earliest_arrival(datetime(2026, 7, 1, 9, 0)) is not None
    # Out of season.
    assert e.earliest_arrival(datetime(2026, 1, 1, 9, 0)) is None


def test_graph_add_and_query_out_edges():
    g = MultimodalGraph()
    g.add_node(_place("a"))
    g.add_node(_place("b"))
    g.add_edge(Edge(id="e", from_node="a", to_node="b", mode=Mode.CAR,
                    static_seconds=60.0))
    assert [e.id for e in g.out_edges("a")] == ["e"]
    assert g.out_edges("b") == []


def test_graph_add_edge_unknown_node_raises():
    g = MultimodalGraph()
    g.add_node(_place("a"))
    with pytest.raises(KeyError):
        g.add_edge(Edge(id="e", from_node="a", to_node="ghost", mode=Mode.CAR,
                        static_seconds=60.0))


def test_active_out_edges_filters_by_season():
    g = MultimodalGraph()
    g.add_node(_place("a"))
    g.add_node(_place("b"))
    g.add_edge(Edge(id="road", from_node="a", to_node="b", mode=Mode.CAR,
                    static_seconds=60.0))
    g.add_edge(Edge(id="ferry", from_node="a", to_node="b", mode=Mode.FERRY,
                    static_seconds=3600.0, validity=Validity(calendar=SUMMER)))
    in_season = g.active_out_edges("a", date(2026, 7, 1))
    out_season = g.active_out_edges("a", date(2026, 1, 1))
    assert {e.id for e in in_season} == {"road", "ferry"}
    assert {e.id for e in out_season} == {"road"}
