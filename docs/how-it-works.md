# travelplanner — how it works

Every itinerary below is the actual value returned by `plan(...)` /
`plan_trip(...)`, produced by the runnable
[`examples/scenarios.py`](../examples/scenarios.py). Run it yourself with
`uv run python examples/scenarios.py` and you get exactly this output.

## What it does

You give it two places and a departure time. It plans the whole journey —
getting to a nearby station or airport, the trains/ferries/flights in between
(with real schedules and seasonal availability), and the trip from the final
stop to your door — and ranks the options by the **objective** you choose:
`FASTEST`, `CHEAPEST`, `FEWEST_TRANSFERS`, `AIR_PRIORITY`, or `GREENEST`.

```
Two places + time  ->  Get to nearby stops  ->  Scan real schedules  ->  Get to the door  ->  Rank by objective  ->  Itineraries
```

It only ever uses scheduled connections that actually run on that date and
season. If there is no way through, it returns an empty list rather than
inventing a sea or air crossing. (The short door-to-stop hops are estimated by
road distance, or by the real road network when you pass `road=True`.)

## How to read the results

You call `plan(origin, dest, departure, timetable, connector, objective=...)`
(or the one-call `plan_trip(...)`) and get back a **ranked list of itineraries**.
Each itinerary is a sequence of **legs** (one printed row = one leg):

- **primary_mode** — the dominant vehicle of the trip (the longest leg).
- **arrive_at / total_duration** — door-to-door arrival time and elapsed time.
- **cost_level** — a relative `low` / `medium` / `high` band, *not* a currency price.
- **transfers** — how many times you switch vehicles.

An itinerary exposes `legs`, `depart_at`, `arrive_at`, `total_duration`,
`primary_mode`, and `cost_level`; each leg has `mode`, `from_loc`, `to_loc`,
`travel_time`, and `overhead` (the wait before it departs).

## Example 1 — a business trip, time matters

> *"I'm in Zurich and need to be in Berlin. Get me there fast."*

The setup (a flight, a 1-change train, and road access at both ends) lives in
`zurich_berlin()`. The call:

```python
tt, conn, origin, dest, depart = zurich_berlin()   # depart 2026-07-01 07:30
results = plan(origin, dest, depart, tt, conn, objective=Objective.AIR_PRIORITY)
```

Output — with `AIR_PRIORITY` the flight is returned first:

```
Zurich -> Berlin (AIR_PRIORITY):
  flight | arrive 11:38 | cost high
    car    Hotel Storchen, Zurich -> Zurich Airport
    flight Zurich Airport -> Berlin Brandenburg
    car    Berlin Brandenburg -> Office in Berlin-Mitte
```

## Example 2 — same trip, but on a budget

> *"Same Zurich to Berlin, but I'd rather save money than time."*

Same timetable, same connector, only the objective changes:

```python
results = plan(origin, dest, depart, tt, conn, objective=Objective.CHEAPEST)
```

Output — the trade-off set is the same, but now the train wins:

```
Zurich -> Berlin (CHEAPEST):
  train | arrive 16:03 | cost medium
    walk   Hotel Storchen, Zurich -> Zurich HB
    train  Zurich HB -> Halle (Saale) Hbf
    train  Halle (Saale) Hbf -> Berlin Hbf
    car    Berlin Hbf -> Office in Berlin-Mitte
```

Nothing about the trip changed except the objective. The engine keeps the whole
trade-off set and just picks differently.

## Example 3 — an island, and a ferry that only runs in summer

> *"We rented a villa on Mljet, off Dubrovnik. There's no bridge — the only way
> across is the catamaran, and it stops for winter."*

The ferry is valid only in months 5–10 (`Validity(open_months=...)`), and the
island is a separate road component (no road across the sea). Booking for July:

```python
tt, conn, origin, dest, summer, winter = dubrovnik_mljet()
results = plan(origin, dest, summer, tt, conn)     # summer = 2026-07-20 14:00
```

```
Dubrovnik -> Mljet (July):
  ferry | arrive 17:15 | cost medium
    car    Old Town apartment, Dubrovnik -> Dubrovnik Port
    ferry  Dubrovnik Port -> Mljet (Sobra)
    car    Mljet (Sobra) -> Villa on Mljet
```

The same trip in January:

```python
results = plan(origin, dest, winter, tt, conn)     # winter = 2026-01-20 14:00
```

```
Dubrovnik -> Mljet (January):
  (no itinerary - the destination is unreachable)
```

**No itinerary.** The ferry doesn't run in January, and the island has no road
across the sea, so the engine returns an empty list rather than a made-up "drive
across the water".

## Choosing the first/last mile

`plan_trip(...)` is the one-call, door-to-door entry point — it geocodes the
endpoints and picks a connector for you. `access` controls the first/last mile:
`access="car"` drives to the airport, `access="transit"` walks to the station and
takes a feeder train.

```python
tt, origin, dest, depart = airport_access()        # Amsterdam -> Vaduz
for access in ("car", "transit"):
    it = plan_trip(origin, dest, depart, tt, access=access)[0]
```

```
plan_trip door-to-door, choosing the first/last mile:
  access=car      car -> flight -> walk, arrive 11:40
  access=transit  walk -> train -> flight -> walk, arrive 11:40
```

The origin/destination can be any `LocationType` (`CITY`, `LANDMARK`, `STATION`,
`AIRPORT`, `HOTEL`); routing is by coordinates and the type is a label — starting
at a station or airport just gives a near-zero access hop.

## No timetable needed

The examples above pass an explicit timetable to show the full setup. In practice
you can omit it: `plan_trip` auto-composes one for the trip — the OpenFlights
flight network plus the GTFS feed(s) selected by location from the Mobility
Database catalog, downloaded and cached on first use.

```python
from travelplanner import plan_trip

results = plan_trip("Amsterdam", "Zurich", depart)   # no timetable
# -> car -> Amsterdam Airport Schiphol -> flight -> Zürich Airport -> car
```

From the CLI it is the default, too: `travelplanner plan "52.37,4.90" "47.38,8.54"`.

The auto-sourced data is third-party: OpenFlights flights under the ODbL, and
GTFS feeds (via the Mobility Database catalog) each under their own license.
`travelplanner attribution ORIGIN DEST` prints the credit and the feeds covering
a trip; see the README's "Data sources and licensing" for the full notice.

## Run it yourself

```bash
git clone https://github.com/bgokden/travelplanner.git && cd travelplanner
uv sync                                  # install (not yet on PyPI)
uv run travelplanner demo                # the bundled sample, every objective
uv run python examples/scenarios.py      # the examples above + the no-timetable run
```

## About these examples

The timetables here are small and illustrative — the engine routes over whatever
schedule/road data you give it (a GTFS feed via `load_timetable(...)`, or the
auto-sourced data above). Times are timezone-aware (computed in UTC, shown in
local time); every stop in these examples is within one timezone. Flight times
are synthetic and the cost band is qualitative, not real fares.
