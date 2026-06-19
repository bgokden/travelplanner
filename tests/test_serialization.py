"""to_dict/to_json on result objects + tabular record helpers."""

import json
from datetime import datetime, timedelta

from travelplanner.models import (
    CostLevel,
    Itinerary,
    Leg,
    Location,
    LocationType,
    Mode,
    itinerary_records,
    leg_records,
)


def _loc(name, lat, lon):
    return Location(name, LocationType.CITY, lat, lon)


def _itin():
    legs = [
        Leg(Mode.WALK, _loc("home", 47.0, 8.0), _loc("hbf", 47.01, 8.01),
            0.5, timedelta(minutes=6), timedelta(), CostLevel.LOW),
        Leg(Mode.TRAIN, _loc("hbf", 47.01, 8.01), _loc("dest hbf", 52.0, 13.0),
            500.0, timedelta(hours=4), timedelta(minutes=20), CostLevel.MEDIUM),
        Leg(Mode.FERRY, _loc("dest hbf", 52.0, 13.0), _loc("island", 52.1, 13.2),
            12.0, timedelta(minutes=40), timedelta(minutes=10), CostLevel.HIGH),
    ]
    return Itinerary(legs, datetime(2026, 7, 1, 9, 0), score=1.0)


def test_location_to_dict():
    assert _loc("X", 1.0, 2.0).to_dict() == {
        "name": "X", "type": "city", "lat": 1.0, "lon": 2.0}


def test_leg_to_dict_is_json_safe():
    leg = _itin().legs[1]
    d = leg.to_dict()
    assert d["mode"] == "train"
    assert d["cost_level"] == "medium"
    assert d["travel_time_s"] == 4 * 3600
    assert d["overhead_s"] == 20 * 60
    assert d["duration_s"] == 4 * 3600 + 20 * 60
    json.dumps(d)  # must not raise


def test_itinerary_to_dict_and_json():
    it = _itin()
    d = it.to_dict()
    assert d["primary_mode"] == "train"      # longest leg
    assert d["num_transfers"] == 1           # train + ferry line-haul -> 1
    assert d["cost_level"] == "high"         # max over legs
    assert d["depart_at"] == "2026-07-01T09:00:00"
    assert abs(d["total_minutes"] - it.total_duration.total_seconds() / 60) < 1e-9
    assert len(d["legs"]) == 3
    parsed = json.loads(it.to_json())        # round-trips through JSON
    assert parsed["num_transfers"] == 1
    assert "legs" not in it.to_dict(with_legs=False)


def test_num_transfers_excludes_ground():
    # walk + train + walk -> single line-haul -> 0 transfers
    legs = [
        Leg(Mode.WALK, _loc("a", 0, 0), _loc("b", 0, 0), 0.1,
            timedelta(minutes=5), timedelta(), CostLevel.LOW),
        Leg(Mode.TRAIN, _loc("b", 0, 0), _loc("c", 1, 1), 100.0,
            timedelta(hours=1), timedelta(), CostLevel.MEDIUM),
        Leg(Mode.CAR, _loc("c", 1, 1), _loc("d", 1, 1), 2.0,
            timedelta(minutes=5), timedelta(), CostLevel.MEDIUM),
    ]
    assert Itinerary(legs, datetime(2026, 1, 1), 1.0).num_transfers == 0


def test_record_helpers():
    results = [_itin(), _itin()]
    irecs = itinerary_records(results)
    assert [r["rank"] for r in irecs] == [0, 1]
    assert "legs" not in irecs[0]
    json.dumps(irecs)

    lrecs = leg_records(results)
    assert len(lrecs) == 6                   # 3 legs x 2 itineraries
    assert lrecs[0]["itinerary"] == 0 and lrecs[0]["leg_index"] == 0
    assert lrecs[3]["itinerary"] == 1
    json.dumps(lrecs)
