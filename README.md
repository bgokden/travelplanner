# travelplanner

[![CI](https://github.com/bgokden/travelplanner/actions/workflows/ci.yml/badge.svg)](https://github.com/bgokden/travelplanner/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

Multimodal, door-to-door travel planning that prioritizes air travel.

Given two locations (hotel, city, landmark, station, airport) and a departure
time, it plans an end-to-end journey across **walking, driving, rail, ferry, and
flight** — resolving access to and from transit, respecting **seasonal and
conditional** edge availability (a winter-closed alpine pass, a summer-only
ferry), and selecting across a **Pareto frontier** of total time, cost, and
number of transfers, with **air prioritized**.

It is built on established route-planning algorithms:

- **Customizable Contraction Hierarchies (CCH/CRP)** for the road layer — a
  season/condition change re-customizes a whole country in ~1 second without
  re-running preprocessing.
- **Connection Scan Algorithm (CSA)** for scheduled transit (rail/ferry/flight).
- **Phased coupling** of the two, which enforces legal mode sequences
  (no driving in the middle of a flight chain).

## Install

```bash
pip install travelplanner
```

The core (scheduled engine, coupling, heuristic estimator) is **pure standard
library**. The road engine is optional:

```bash
pip install "travelplanner[road]"   # adds routingkit-cch (CCH) + osmium (OSM)
```

> Note: `routingkit-cch` builds from source and needs a C++17 compiler with
> OpenMP. The geometric connector works without it.

## Quick start

Try the bundled sample — no data required:

```bash
travelplanner demo
```

In code, `plan_trip` is the one-call, door-to-door entry point: give it two
locations (a name, a `"lat,lon"` string, a tuple, or a `Location`), a departure
time, and a `Timetable`. It geocodes the endpoints, picks a connector, and plans
the whole journey — you do not hand-build a connector. The package ships a sample
so you can run immediately:

```python
from travelplanner import plan_trip, Objective, sample_timetable, sample_trip

tt = sample_timetable()
origin, dest, depart = sample_trip()

for it in plan_trip(origin, dest, depart, tt, objective=Objective.AIR_PRIORITY):
    print(it.primary_mode.value, it.arrive_at, it.cost_level.value)
    for leg in it.legs:
        print("  ", leg.mode.value, leg.from_loc.name, "->", leg.to_loc.name)
```

Switch the objective to `FASTEST`, `CHEAPEST`, `FEWEST_TRANSFERS`, or `GREENEST`
(least private-car distance) to see the frontier reorder.

**Choosing how the first/last mile works:**

```python
# Real road network for access/egress (one region auto-selected from the
# coordinates; a trip spanning two regions auto-splits per endpoint):
plan_trip(origin, dest, depart, tt, road=True)
plan_trip(origin, dest, depart, tt, road=True, turn_aware=True)  # turn restrictions + junction costs

# Prefer public transport for the first/last mile, like a "Transit" tab:
# walk to the nearest stop and take the train to the airport instead of driving.
plan_trip(origin, dest, depart, tt, access="transit")
```

`plan(origin, dest, depart, tt, connector, ...)` remains available as the
lower-level call when you want to build and pass a specific `RoadConnector`
yourself.

## Using your own data

Supply a GTFS feed as the `Timetable` and let `plan_trip` do the rest:

```python
from travelplanner import load_timetable, plan_trip

tt = load_timetable("path/to/gtfs_feed/")     # GTFS: stops, routes, trips,
                                              # stop_times, calendar(_dates)
results = plan_trip("Amsterdam", "Vaduz", depart, tt)            # straight-line access
results = plan_trip("Amsterdam", "Vaduz", depart, tt, road=True) # real road access
```

Transit feeds are not auto-discovered (unlike road extracts): you supply the
feed, and transit quality is feed quality. With `road=True` the road extract is
auto-selected from the coordinates and cached; a cross-region trip resolves a
separate extract per endpoint (a `SplitConnector`), and a trip no single extract
covers falls back to straight-line access rather than loading a continent.

For full manual control you can still build a connector and call `plan` directly:

```python
from travelplanner.graph.road.osm import load_road_graph
from travelplanner.graph.road import CCHRoadRouter
from travelplanner import CCHConnector, plan

road = CCHRoadRouter(load_road_graph("region.osm.pbf"))
conn = CCHConnector(road, tt.stops, stop_to_node={...})
results = plan(origin, dest, depart, tt, conn)
```

## CLI

```bash
travelplanner demo                              # bundled sample, all objectives
travelplanner plan "London" "Paris"             # over the bundled sample timetable
travelplanner plan "47.0,7.0" "45.0,9.0" --gtfs feed/ --objective cheapest
```

`plan` takes a `lat,lon` or a bundled city name for origin/destination; with no
`--gtfs` it uses the bundled sample timetable.

## How air priority works

`AIR_PRIORITY` prefers a flight **among non-dominated options**: if a flight is
faster, or has fewer transfers, or is the only way, it is chosen; a flight that
is strictly worse on time *and* cost *and* transfers is dropped. This is more
principled than a fixed "air bonus" that could pick a strictly worse flight.

The Pareto frontier trades off four axes — total time, cost, transfers, and
private-car distance — and each objective just reorders that one frontier.
`GREENEST` ranks by least driving, so a walk-and-train option is preferred over a
faster drive-to-airport flight; the other objectives are unaffected.

## Limitations

- Times are treated as **naive local times** — multi-timezone international
  flights are not yet handled correctly.
- No bundled live flight schedules; supply your own GTFS/timetable data.
- Country-scale road graphs are memory-heavy (node bookkeeping); fine for a
  region, large for a continent.

## Learn more

- A visual walkthrough with worked, real-output examples:
  [`docs/how-it-works.html`](docs/how-it-works.html) (rendered preview:
  `docs/how-it-works.png`).
- Runnable example scenarios (full setup visible):
  [`examples/scenarios.py`](examples/scenarios.py) — `python examples/scenarios.py`.

## License

MIT — see [LICENSE](LICENSE).
