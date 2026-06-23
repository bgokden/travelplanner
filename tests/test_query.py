from datetime import datetime

import pytest

from travelplanner import place
from travelplanner.models import LocationType
from travelplanner.graph.query import Objective, TravelQuery

ORIGIN = place("Hotel A", LocationType.HOTEL, 40.0, -74.0)
DEST = place("Airport B", LocationType.AIRPORT, 35.0, 139.0)


def test_query_defaults():
    q = TravelQuery(origin=ORIGIN, dest=DEST,
                    depart_after=datetime(2026, 7, 1, 8, 0))
    assert q.objective is Objective.FASTEST
    assert q.arrive_before is None
    assert q.conditions == frozenset()
    assert q.top_n == 3


def test_query_rejects_inverted_window():
    with pytest.raises(ValueError):
        TravelQuery(origin=ORIGIN, dest=DEST,
                    depart_after=datetime(2026, 7, 1, 10, 0),
                    arrive_before=datetime(2026, 7, 1, 9, 0))


def test_query_rejects_bad_top_n():
    with pytest.raises(ValueError):
        TravelQuery(origin=ORIGIN, dest=DEST,
                    depart_after=datetime(2026, 7, 1, 8, 0), top_n=0)


def test_query_accepts_conditions():
    q = TravelQuery(origin=ORIGIN, dest=DEST,
                    depart_after=datetime(2026, 7, 1, 8, 0),
                    conditions=frozenset({"weather_good", "pass_open"}),
                    objective=Objective.FASTEST)
    assert "pass_open" in q.conditions
    assert q.objective is Objective.FASTEST
