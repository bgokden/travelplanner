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
`FASTEST` (the default), `CHEAPEST`, `FEWEST_TRANSFERS`, `AIR_PRIORITY`, or
`GREENEST`.

```
Two places + time  ->  Get to nearby stops  ->  Scan real schedules  ->  Get to the door  ->  Rank by objective  ->  Itineraries
```

It only ever uses scheduled connections that actually run on that date and
season. If there is no way through, it returns an empty list rather than
inventing a sea or air crossing. (The short door-to-stop hops are estimated by
road distance, or by the real road network when you pass `road=True`.)

## How to read the results

You call `plan(origin, dest, departure, timetable, connector, objective=...)`
(or the one-call `plan_trip(...)`) and get back a **ranked list of itineraries**,
each one a route card you can render directly — a top line plus one row per leg:

```
flight | 4h 8m | arrive 11:38 | ~96 EUR | cost high
  07:30-07:43  Drive to Zurich Airport
  10:00-11:15  Flight from Zurich Airport to Berlin Brandenburg
  11:15-11:38  Drive to Office in Berlin-Mitte
```

- **primary_mode** — the dominant vehicle of the trip (the longest leg).
- **total_duration_human** — door-to-door elapsed time, formatted ("4h 8m").
- **arrive_at** — door-to-door arrival (each leg also carries its own local clock).
- **fare_estimate / fare_currency** — a rough representative cost, shown with a
  leading `~`. An estimate for ranking and a ballpark, **not a quoted fare** (see
  "What the fare is" below).
- **cost_level** — a coarse `low` / `medium` / `high` band, for display.
- **num_transfers** — how many times you switch vehicles.

Each itinerary exposes `legs`, `depart_at`, `arrive_at`, `total_duration`
(plus `total_duration_human`), `primary_mode`, `cost_level`, `num_transfers`, and
`fare_estimate` / `fare_currency`. Each leg has `mode`, `from_loc`, `to_loc`,
absolute `depart_at` / `arrive_at` (local to its endpoints), `travel_time`,
`overhead` (the wait before it), `duration_human`, a one-line `describe()`, its own
`fare_estimate`, and — for a road-routed car leg — `geometry`, the routed polyline.
`to_dict()` / `to_json()` give all of this JSON-safe.

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
  flight | 4h 8m | arrive 11:38 | ~96 EUR | cost high
    07:30-07:43  Drive to Zurich Airport
    10:00-11:15  Flight from Zurich Airport to Berlin Brandenburg
    11:15-11:38  Drive to Office in Berlin-Mitte
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
  train | 8h 33m | arrive 16:03 | ~86 EUR | cost medium
    07:30-07:39  Walk to Zurich HB
    08:00-14:30  Train from Zurich HB to Halle (Saale) Hbf
    14:50-16:00  Train from Halle (Saale) Hbf to Berlin Hbf
    16:00-16:03  Drive to Office in Berlin-Mitte
```

Nothing about the trip changed except the objective. The engine keeps the whole
trade-off set and just picks differently — here `CHEAPEST` ranks on the
`fare_estimate` (the ~86 EUR train over the ~96 EUR flight), not just the coarse
`cost_level` band.

### What the fare is

`fare_estimate` is a **rough representative cost**, not a quoted ticket price: a
distance-and-mode heuristic (`travelplanner.fares`) that ignores discounts, daily
caps, transfer rules, advance-purchase and peak pricing — so it can overestimate a
multi-leg transit day and overprice a short budget flight. It exists to give
`CHEAPEST` a continuous number to order on (better than the 3-level band) and a
ballpark to show. Swap or disable it with `set_fare_model` / `free_model`; supply a
data-backed model for real numbers.

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
  ferry | 3h 15m | arrive 17:15 | ~12 EUR | cost medium
    14:00-14:10  Drive to Dubrovnik Port
    16:00-17:00  Ferry from Dubrovnik Port to Mljet (Sobra)
    17:00-17:15  Drive to Villa on Mljet
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

## Driving times respond to the departure

With `road=True` the car legs are routed over the real street network and timed by
a speed model that reads the departure: the same drive is slower in the weekday
rush hour and quicker at night (an average-congestion model is the default;
free-flow is opt-in). The road-routed car leg also carries `geometry`, the routed
polyline, so a UI can draw the actual path.

```
Time-of-day driving (road=True over a classified road):
  depart rush 08:00     -> drive 38m
  depart off-peak 13:00 -> drive 26m
  depart night 03:00    -> drive 25m
```

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
are synthetic, and the fare is a rough distance-and-mode estimate (not a quoted
price); supply your own schedule data and fare model for real numbers.
