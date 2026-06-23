"""to_dict/to_json on result objects + tabular record helpers."""

import json
from dataclasses import replace
from datetime import datetime, timedelta

from travelplanner.models import (
    CostLevel,
    Itinerary,
    Leg,
    Location,
    LocationType,
    Mode,
    humanize_duration,
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


def test_humanize_duration():
    assert humanize_duration(timedelta(hours=2, minutes=9, seconds=26)) == "2h 9m"
    assert humanize_duration(timedelta(minutes=45)) == "45m"
    assert humanize_duration(timedelta()) == "0m"
    assert humanize_duration(timedelta(seconds=30)) == "0m"    # sub-minute floors
    assert humanize_duration(timedelta(hours=1)) == "1h"       # exact hour, no "0m"
    assert humanize_duration(timedelta(days=1, hours=3, minutes=5)) == "1d 3h 5m"


def test_to_dict_includes_human_durations():
    it = _itin()                               # 6m + 4h20m + 50m = 5h 16m total
    assert it.to_dict()["total_duration_human"] == "5h 16m"
    assert it.legs[1].to_dict()["duration_human"] == "4h 20m"


def test_human_duration_properties():
    it = _itin()
    assert it.total_duration_human == "5h 16m"
    assert it.legs[1].duration_human == "4h 20m"


def test_legs_get_stamped_absolute_times():
    it = _itin()                               # depart 09:00
    l0, l1, l2 = it.legs
    assert l0.depart_at == datetime(2026, 7, 1, 9, 0)      # walk, no wait
    assert l0.arrive_at == datetime(2026, 7, 1, 9, 6)
    assert l1.depart_at == datetime(2026, 7, 1, 9, 26)     # 20m wait, then 4h
    assert l1.arrive_at == datetime(2026, 7, 1, 13, 26)
    assert l2.arrive_at == it.arrive_at                    # last arrival == trip arrival
    d = l1.to_dict()
    assert d["depart_at"] == "2026-07-01T09:26:00"
    assert d["arrive_at"] == "2026-07-01T13:26:00"


def test_leg_describe_and_summary():
    l0, l1, l2 = _itin().legs
    assert l0.describe() == "Walk to hbf"                  # ground: names the dest
    assert l1.describe() == "Train from hbf to dest hbf"   # line-haul: names both ends
    assert l2.describe() == "Ferry from dest hbf to island"
    assert l1.to_dict()["summary"] == "Train from hbf to dest hbf"


def test_to_dict_times_drop_microseconds():
    # Summed float-second durations give spurious microseconds; clock strings must
    # not carry them (a leaky "this is a float, not a time" footgun).
    legs = [Leg(Mode.WALK, _loc("a", 0, 0), _loc("b", 0, 0), 0.4,
                timedelta(seconds=273.5), timedelta(), CostLevel.LOW)]
    d = Itinerary(legs, datetime(2026, 7, 1, 8, 0), 1.0).to_dict()
    assert d["arrive_at"] == "2026-07-01T08:04:33"        # .5s dropped
    assert d["legs"][0]["arrive_at"] == "2026-07-01T08:04:33"
    assert "." not in d["arrive_at"]


def test_freestanding_leg_has_no_stamped_times():
    leg = Leg(Mode.WALK, _loc("a", 0, 0), _loc("b", 0, 0), 0.1,
              timedelta(minutes=5), timedelta(), CostLevel.LOW)
    assert leg.depart_at is None and leg.arrive_at is None
    assert "depart_at" not in leg.to_dict()


def test_stamping_is_copy_on_stamp_and_idempotent():
    # Itineraries stamp COPIES, never the caller's Leg objects, and restamp purely
    # from depart_at -- so reconstructing or dataclasses.replace()-ing an itinerary
    # cannot layer stale clock times onto fresh ones.
    legs = [
        Leg(Mode.WALK, _loc("a", 0, 0), _loc("b", 0, 0), 0.4,
            timedelta(minutes=5), timedelta(), CostLevel.LOW),
        Leg(Mode.TRAIN, _loc("b", 0, 0), _loc("c", 1, 1), 100.0,
            timedelta(hours=1), timedelta(minutes=10), CostLevel.MEDIUM),
    ]
    it = Itinerary(legs, datetime(2026, 7, 1, 9, 0), 1.0)
    # The caller's own legs are left untouched (no aliasing surprise).
    assert legs[0].depart_at is None and legs[0].arrive_at is None
    assert it.legs[0].depart_at == datetime(2026, 7, 1, 9, 0)
    # Restamping already-stamped legs yields the same times (pure in depart_at).
    again = Itinerary(it.legs, datetime(2026, 7, 1, 9, 0), 1.0)
    assert [leg.depart_at for leg in again.legs] == [leg.depart_at for leg in it.legs]
    assert [leg.arrive_at for leg in again.legs] == [leg.arrive_at for leg in it.legs]
    # replace() with a new departure restamps from it, not layered on prior stamps.
    shifted = replace(it, depart_at=datetime(2026, 7, 1, 10, 0))
    assert shifted.legs[0].depart_at == datetime(2026, 7, 1, 10, 0)
    assert shifted.legs[1].arrive_at == datetime(2026, 7, 1, 11, 15)  # +5m +10m +1h


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
