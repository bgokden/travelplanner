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

In code, the multimodal planner needs a `Timetable` and a `RoadConnector`. The
package ships a sample so you can run immediately:

```python
from travelplanner import plan, GeometricConnector, Objective, sample_timetable, sample_trip

tt = sample_timetable()
conn = GeometricConnector(tt.stops)
origin, dest, depart = sample_trip()

for it in plan(origin, dest, depart, tt, conn, objective=Objective.AIR_PRIORITY):
    print(it.primary_mode.value, it.arrive_at, it.cost_level.value)
    for leg in it.legs:
        print("  ", leg.mode.value, leg.from_loc.name, "->", leg.to_loc.name)
```

Switch the objective to `FASTEST`, `CHEAPEST`, or `FEWEST_TRANSFERS` to see the
frontier reorder.

## Using your own data

```python
from travelplanner import load_timetable, GeometricConnector, plan

tt = load_timetable("path/to/gtfs_feed/")     # GTFS: stops, routes, trips,
                                              # stop_times, calendar(_dates)
conn = GeometricConnector(tt.stops)           # straight-line access/egress
results = plan(origin, dest, depart, tt, conn)
```

For street-accurate access/egress over a real road network, build a CCH router
from an OpenStreetMap extract and use `CCHConnector` (requires the `road` extra):

```python
from travelplanner.graph.road.osm import load_road_graph
from travelplanner.graph.road import CCHRoadRouter
from travelplanner import CCHConnector, plan

road = CCHRoadRouter(load_road_graph("region.osm.pbf"))
conn = CCHConnector(road, tt.stops, stop_to_node={...})
results = plan(origin, dest, depart, tt, conn)
```

## Quick heuristic estimate

For a zero-setup guess from just two locations (bundled airport/city tables, no
timetable, estimates rather than schedules):

```python
from datetime import datetime
from travelplanner import estimate, city

for it in estimate(city("New York"), city("Tokyo"), datetime(2026, 7, 1, 8, 0)):
    print(it.primary_mode.value, it.total_duration, it.cost_level.value)
```

or from the CLI:

```bash
travelplanner estimate "London" "Paris" --at 2026-07-01T08:00
```

## How air priority works

`AIR_PRIORITY` prefers a flight **among non-dominated options**: if a flight is
faster, or has fewer transfers, or is the only way, it is chosen; a flight that
is strictly worse on time *and* cost *and* transfers is dropped. This is more
principled than a fixed "air bonus" that could pick a strictly worse flight.

## Limitations

- Times are treated as **naive local times** — multi-timezone international
  flights are not yet handled correctly.
- No bundled live flight schedules; supply your own GTFS/timetable data.
- Country-scale road graphs are memory-heavy (node bookkeeping); fine for a
  region, large for a continent.

## License

MIT — see [LICENSE](LICENSE).
