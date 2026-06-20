"""Build a flight Timetable from OpenFlights data (airports + route network).

OpenFlights (https://openflights.org/data.html) publishes an open airport list
and a route network -- which airline flies A -> B -- but NO schedules (no times,
no frequencies). This loader turns that into a usable Timetable: each airport
becomes an AIRPORT Stop and each directed route becomes one or more daily flights
whose duration is estimated from the great-circle distance. The schedule is
therefore SYNTHETIC (a representative few departures per day at a plausible cruise
speed), not a real airline timetable -- it fills the "no bundled flight schedules"
gap so the air line-haul can route over real airports and real route geography.

    from travelplanner.openflights import load_openflights
    tt = load_openflights(download=True, keep={"AMS", "ZRH", "LHR"})

The full dataset is large (~7700 airports, ~37k directed routes); restrict it
with `keep` (a set of IATA codes) to keep the Timetable -- and the scan -- small.
"""

import csv
import os
import urllib.request
from datetime import timedelta

from travelplanner.geo import haversine
from travelplanner.models import CostLevel, Mode
from travelplanner.graph.schema import NodeType
from travelplanner.graph.scheduled.model import Stop, StopTime, Timetable, Trip

AIRPORTS_URL = ("https://raw.githubusercontent.com/jpatokal/openflights/"
                "master/data/airports.dat")
ROUTES_URL = ("https://raw.githubusercontent.com/jpatokal/openflights/"
              "master/data/routes.dat")

# Synthetic-schedule defaults: a fixed per-flight overhead (taxi/climb/descent)
# plus cruise time from great-circle distance, departing a few times a day.
DEFAULT_CRUISE_KMH = 800.0
DEFAULT_OVERHEAD = timedelta(minutes=45)
DEFAULT_DEPART_HOURS = (6, 10, 14, 18)

_NULL = {"", "\\N", "\\n"}


def _download(url: str) -> str:
    """Fetch an OpenFlights .dat to the shared cache and return its local path."""
    from travelplanner.roads import cache_dir

    dest = os.path.join(cache_dir(), "openflights-" + url.rsplit("/", 1)[-1])
    if not os.path.exists(dest):
        tmp = dest + ".part"
        req = urllib.request.Request(url, headers={"User-Agent": "travelplanner"})
        with urllib.request.urlopen(req) as resp, open(tmp, "wb") as out:
            out.write(resp.read())
        os.replace(tmp, dest)
    return dest


def _airports(path: str, keep) -> dict[str, Stop]:
    """IATA code -> AIRPORT Stop, for airports that have an IATA code and coords."""
    out: dict[str, Stop] = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.reader(f):
            if len(row) < 8:
                continue
            iata = row[4].strip()
            if iata in _NULL or (keep is not None and iata not in keep):
                continue
            try:
                lat, lon = float(row[6]), float(row[7])
            except ValueError:
                continue
            out[iata] = Stop(id=iata, name=row[1].strip() or iata,
                             lat=lat, lon=lon, type=NodeType.AIRPORT)
    return out


def _route_pairs(path: str, airports: dict[str, Stop]) -> set[tuple[str, str]]:
    """Distinct directed (src, dst) IATA pairs for non-stop routes between two
    known airports (codeshares collapse to one network edge)."""
    pairs: set[tuple[str, str]] = set()
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.reader(f):
            if len(row) < 8:
                continue
            src, dst, stops = row[2].strip(), row[4].strip(), row[7].strip()
            if stops != "0":                       # only non-stop legs
                continue
            if src in airports and dst in airports and src != dst:
                pairs.add((src, dst))
    return pairs


def _flight_duration(a: Stop, b: Stop, cruise_kmh: float,
                     overhead: timedelta) -> timedelta:
    km = haversine(a.lat, a.lon, b.lat, b.lon)
    return overhead + timedelta(hours=km / cruise_kmh)


def load_openflights(airports: str | None = None, routes: str | None = None, *,
                     keep=None, depart_hours=DEFAULT_DEPART_HOURS,
                     cruise_kmh: float = DEFAULT_CRUISE_KMH,
                     overhead: timedelta = DEFAULT_OVERHEAD,
                     download: bool = False) -> Timetable:
    """A Timetable of synthetic daily flights over the OpenFlights network.

    `airports`/`routes` are local paths to airports.dat / routes.dat; with
    `download=True` they are fetched from OpenFlights and cached. `keep` (an
    iterable of IATA codes) restricts to those airports and the routes between
    them -- use it, the full network is large. Each directed non-stop route
    becomes one Trip per hour in `depart_hours`; flight time is `overhead` plus
    great-circle distance / `cruise_kmh`. Cost level is HIGH (air). The schedule
    is synthetic: OpenFlights has no real times.
    """
    keep = set(keep) if keep is not None else None
    if airports is None:
        if not download:
            raise ValueError("pass airports=/routes= paths, or download=True")
        airports = _download(AIRPORTS_URL)
    if routes is None:
        routes = _download(ROUTES_URL) if download else None
    if routes is None:
        raise ValueError("pass routes= path, or download=True")

    stops = _airports(airports, keep)
    pairs = _route_pairs(routes, stops)

    tt = Timetable()
    used: set[str] = set()
    for src, dst in sorted(pairs):
        a, b = stops[src], stops[dst]
        dur = _flight_duration(a, b, cruise_kmh, overhead)
        for hour in depart_hours:
            dep = timedelta(hours=hour)
            tt.add_trip(Trip(
                id=f"{src}-{dst}@{hour:02d}", mode=Mode.FLIGHT,
                stop_times=(StopTime(src, dep, dep),
                            StopTime(dst, dep + dur, dep + dur)),
                cost_level=CostLevel.HIGH))
        used.add(src)
        used.add(dst)

    for iata in used:                              # only airports that have flights
        tt.add_stop(stops[iata])
    return tt
