"""On-disk artifact for a composed Timetable (offline transit deployment).

`build_default_timetable` composes a trip's timetable by downloading and merging
the flight network and the covering GTFS feed(s) -- network-dependent work redone
each run. This serializes the composed result to a single JSON file so an offline
runtime loads it directly. A corridor timetable is small (hundreds to a few
thousand trips), so JSON is compact enough and keeps the artifact legible; the
road layer uses packed binary only because it is country-scale.

Durations (stop-time offsets, footpaths, transfer times) are stored as float
seconds so the timetable round-trips exactly -- a synthetic flight's offset can
carry sub-second precision.

    save_timetable(tt, path)
    tt = load_timetable_artifact(path)
"""

import json
from datetime import timedelta

from travelplanner.models import CostLevel, Mode
from travelplanner.graph.schema import NodeType
from travelplanner.graph.scheduled.model import (
    Stop, StopTime, Timetable, Trip)
from travelplanner.graph.validity import validity_from_json, validity_to_json

FORMAT_VERSION = 1


def _dur(td: timedelta | None):
    return None if td is None else td.total_seconds()


def _td(value):
    return None if value is None else timedelta(seconds=value)


def _stop_to_json(s: Stop) -> dict:
    return {"id": s.id, "name": s.name, "lat": s.lat, "lon": s.lon,
            "type": s.type.value, "min_transfer": _dur(s.min_transfer),
            "tz": s.tz}


def _stop_from_json(o) -> Stop:
    return Stop(id=o["id"], name=o["name"], lat=o["lat"], lon=o["lon"],
                type=NodeType(o["type"]), min_transfer=_td(o["min_transfer"]),
                tz=o["tz"])


def _trip_to_json(t: Trip) -> dict:
    return {"id": t.id, "mode": t.mode.value, "cost_level": t.cost_level.value,
            "validity": validity_to_json(t.validity),
            "stop_times": [[st.stop_id, st.arrival.total_seconds(),
                            st.departure.total_seconds()]
                           for st in t.stop_times]}


def _trip_from_json(o) -> Trip:
    return Trip(
        id=o["id"], mode=Mode(o["mode"]), cost_level=CostLevel(o["cost_level"]),
        validity=validity_from_json(o["validity"]),
        stop_times=tuple(
            StopTime(stop_id=s, arrival=timedelta(seconds=a),
                     departure=timedelta(seconds=d))
            for s, a, d in o["stop_times"]))


def save_timetable(tt: Timetable, path: str) -> str:
    """Write a composed Timetable to `path` as JSON; return the path."""
    data = {
        "format_version": FORMAT_VERSION,
        "stops": [_stop_to_json(s) for s in tt.stops.values()],
        "trips": [_trip_to_json(t) for t in tt.trips.values()],
        "footpaths": [[f.from_stop, f.to_stop, f.duration.total_seconds()]
                      for f in tt.footpaths],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return path


def load_timetable_artifact(path: str) -> Timetable:
    """Load a Timetable written by save_timetable (raises on a stale format)."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if data.get("format_version") != FORMAT_VERSION:
        raise ValueError(
            f"timetable artifact format {data.get('format_version')} != "
            f"supported {FORMAT_VERSION}; rebuild it")
    tt = Timetable()
    for o in data["stops"]:
        tt.add_stop(_stop_from_json(o))
    for o in data["trips"]:
        tt.add_trip(_trip_from_json(o))
    for frm, to, dur in data["footpaths"]:
        tt.add_footpath(frm, to, timedelta(seconds=dur))
    return tt
