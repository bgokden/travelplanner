# travelplanner

[![CI](https://github.com/bgokden/travelplanner/actions/workflows/ci.yml/badge.svg)](https://github.com/bgokden/travelplanner/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

Multimodal, door-to-door travel planning.

Given two locations (hotel, city, landmark, station, airport) and a departure
time, it plans an end-to-end journey across **walking, driving, rail, ferry, and
flight** — resolving access to and from transit, respecting **seasonal and
conditional** edge availability (a winter-closed alpine pass, a summer-only
ferry), and selecting across a **Pareto frontier** of total time, cost, and
number of transfers.

It is built on established route-planning algorithms:

- **Customizable Contraction Hierarchies (CCH/CRP)** for the road layer — a
  season/condition change re-customizes a whole country in ~1 second without
  re-running preprocessing.
- **Connection Scan Algorithm (CSA)** for scheduled transit (rail/ferry/flight).
- **Phased coupling** of the two, which enforces legal mode sequences
  (no driving in the middle of a flight chain).

## Install

This is not yet published on PyPI (the `travelplanner` name there is an
unrelated package). Install from a clone of this repository with
[uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/bgokden/travelplanner.git
cd travelplanner
uv sync                        # create the env and install all dependencies
uv run travelplanner demo      # verify the install
uv run pytest                  # run the test suite
```

**Requirements:**

- **Python 3.10+**.
- **A C++17 compiler with OpenMP** (GCC or Clang). Two core dependencies,
  `routingkit-cch` and `osmium`, build from source during install, so the toolchain
  is needed for *every* install -- not only when you use the road engine. It is
  standard on Linux and CI; on a minimal machine install build tools first (e.g.
  `build-essential` on Debian/Ubuntu, `xcode-select --install` on macOS).

Everything else is included -- there are no optional extras. One environment gets
the scheduled engine, the road engine (CCH over OpenStreetMap), the geometric
connector, and calendar-aware speed models, so every feature works out of the box
and nothing has to be enabled separately.

## Quick start

Try the bundled sample — no data required:

```bash
uv run travelplanner demo
```

In code, `plan_trip` is the one-call, door-to-door entry point: give it two
locations (a name, a `"lat,lon"` string, a tuple, or a `Location`) and a
departure time. It geocodes the endpoints, picks a connector, and plans the whole
journey — you do not hand-build a connector. The `Timetable` is optional: omit it
and one is auto-composed for the trip (see "No data needed" below). The package
also ships a sample timetable so you can run fully offline:

```python
from travelplanner import plan_trip, Objective, sample_timetable, sample_trip

tt = sample_timetable()
origin, dest, depart = sample_trip()

for it in plan_trip(origin, dest, depart, tt, objective=Objective.FASTEST):  # ranked; the default
    print(f"{it.primary_mode.value}  {it.total_duration_human}  "
          f"arrive {it.arrive_at:%H:%M}  ({it.cost_level.value})")
    for leg in it.legs:
        print(f"  {leg.depart_at:%H:%M}-{leg.arrive_at:%H:%M}  {leg.describe()}")
```

Switch the objective with `objective=Objective.CHEAPEST` (or `FASTEST`,
`FEWEST_TRANSFERS`, `GREENEST` for least private-car distance, or `AIR_PRIORITY`)
to see the frontier reorder.

**What you get back:** each result is an `Itinerary` that renders like a route
card. The top line is `primary_mode`, `total_duration_human` ("2h 9m"),
`arrive_at`, `num_transfers`, `cost_level` (a relative low/medium/high band), and
`fare_estimate`/`fare_currency` -- a rough representative cost from a
distance-and-mode heuristic (an estimate for ranking and a ballpark, **not a quoted
fare**: it ignores discounts, daily caps, transfer rules, and advance-purchase
pricing; swap or disable it via `travelplanner.fares`). Each `leg` carries the same
`fare_estimate` plus absolute `depart_at`/`arrive_at`
(local to its endpoints via `from_loc.tz`/`to_loc.tz`), a `describe()` step
("Flight from Schiphol to Zurich Airport"), and `from_loc`/`to_loc` with
`lat`/`lon`. A road-backed car leg (`road=True`) also carries `geometry` -- the
routed polyline as `(lat, lon)` points along the real street network; walk,
straight-line, and transit legs leave it `None` (their path is just
`from_loc -> to_loc`). `it.to_dict()`/`it.to_json()` give the same data JSON-safe,
and `itinerary_records(results)` is a pandas-ready table.

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

With `road=True`, driving times are **time-of-day aware**: the road metric is
customized for the departure, so access/egress car legs slow down in the weekday
rush hour and ease off at night (an average-congestion model is the default;
free-flow is opt-in). The straight-line default uses a distance-banded speed
estimate without time-of-day.

`plan(origin, dest, depart, tt, connector, ...)` remains available as the
lower-level call when you want to build and pass a specific `RoadConnector`
yourself.

## No data needed: auto-sourced timetables

Omit the timetable and `plan_trip` composes one for the trip: the OpenFlights
flight network (airports near the endpoints, plus major hub airports so a trip
with no direct flight can connect through a hub) plus the GTFS feed(s)
whose coverage area spans the route, selected from the Mobility Database catalog
and downloaded/cached on first use. Stale cache is refreshed the next time it is
loaded (transit after a week, flights after a month); a refresh that fails offline
falls back to the cached copy, so a network blip never breaks an otherwise-usable
cache.

```python
from travelplanner import plan_trip

results = plan_trip("Amsterdam", "Zurich")   # departs now, timetable auto-composed
```

From the CLI, the same is the default — `travelplanner plan "52.37,4.90"
"47.38,8.54"` returns a car -> Schiphol -> flight -> Zürich Airport -> car trip
with nothing to set up.

Caveats worth knowing: GTFS coverage is uneven by region (strong in Europe and
North America), gaps are reported as warnings, and the flight schedule is
synthetic (real airports and routes, but representative times, not live airline
schedules). For exact, reproducible data, supply a feed instead.

## Using your own data

Supply a GTFS feed as the `Timetable` and let `plan_trip` do the rest:

```python
from travelplanner import load_timetable, plan_trip

tt = load_timetable("path/to/gtfs_feed/")     # GTFS: stops, routes, trips,
                                              # stop_times, calendar(_dates)
results = plan_trip("Amsterdam", "Zurich", timetable=tt)            # straight-line access
results = plan_trip("Amsterdam", "Zurich", timetable=tt, road=True) # real road access
```

When you supply a feed, transit quality is feed quality (no auto-sourcing or
corridor clipping is applied -- you get exactly that feed). With `road=True` the road extract is
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
travelplanner demo                              # bundled offline sample, all objectives
travelplanner plan "47.0,7.0" "45.0,9.0"        # auto timetable (flights + GTFS by location)
travelplanner plan "47.0,7.0" "45.0,9.0" --gtfs feed/ --objective cheapest  # your own feed
travelplanner transit-prefetch "47.0,7.0" "45.0,9.0"  # cache a trip's data for offline use
travelplanner transit-build "47.0,7.0" "45.0,9.0" trip.json  # save a composed timetable artifact
travelplanner plan "47.0,7.0" "45.0,9.0" --timetable trip.json  # plan offline from the artifact
```

From a source checkout (no global install), prefix with `uv run`, e.g.
`uv run travelplanner demo`.

`plan` takes a `lat,lon` or a bundled city name for origin/destination; with no
`--gtfs` it uses the bundled sample timetable.

## Demo web app (map UI)

An interactive map UI for trying it in the browser. It is pure standard-library
`http.server` (no extra dependencies):

```bash
python -m travelplanner.service                       # http://127.0.0.1:8000
python -m travelplanner.service --region switzerland  # real streets for car legs
python -m travelplanner.service --offline             # bundled tables only, no network
```

Start typing an origin and destination to get **autocomplete** suggestions across
bundled cities, **airports** (by name or IATA code), **transit stations** from the
loaded feed, and OpenStreetMap **places** — or paste `lat,lon`. Pick a **preferred
way of transportation** (public transit by default — it remembers your choice) and
the trip comes back as a few **choices labelled by purpose** (Fastest / Cheapest /
Greenest / Fewest changes) you can re-sort, drawn on the map — flight legs as the
great-circle arcs they actually fly, with a mode-colour legend; the transit-first
preferences lead with the train when there's a same-day one. Or click a **ready-made
example** (Amsterdam → Berlin by train, London → New York, …) to fill it all in. Tick
*real streets* with a region to route car legs over the actual road network.

The same thing is a JSON API you can call headless:

- `GET /api/plan?origin=&dest=&depart=&prefer=&top=&road=&transit=&region=` —
  itineraries labelled by purpose, each with per-leg map segments
- `GET /api/geocode?q=` — location autocomplete suggestions
- `GET /api/examples` — the selectable example trips · `GET /api/example` · `GET /api/health`

Online place search uses OpenStreetMap **Nominatim** (debounced, cached, throttled
to ~1 request/second per their usage policy); `--offline` keeps everything to the
bundled tables. The server is single-threaded because the road routers are
thread-affine, so a country-scale road build blocks other requests until it
finishes.

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

- International trips are **timezone-correct end to end**: connections are
  materialized in UTC from each stop's IANA zone, and each leg renders in its own
  local time (leave Amsterdam 10:00, land New York 12:00). The one residual case
  is a pure drive/walk trip that crosses a zone with no transit stop between the
  ends — it is shown in the origin's zone, as there is no stop to read the
  destination zone from.
- Flight schedules are synthetic (real airports and routes from OpenFlights, but
  representative times, not live airline schedules); auto-sourced GTFS coverage
  is uneven by region. Supply your own feed for exact data.
- Country-scale road graphs are still memory-heavy, though the columns are packed
  tight (float32 coordinates, 16-bit interned indices); fine for a region, large
  for a continent.

## Learn more

- A walkthrough with worked, real-output examples:
  [`docs/how-it-works.md`](docs/how-it-works.md).
- Runnable example scenarios (full setup visible):
  [`examples/scenarios.py`](examples/scenarios.py) — `uv run python examples/scenarios.py`.

## Data sources and licensing

travelplanner's own code is MIT. The data it auto-fetches is third-party and
carries its own licenses — credit these when you use or redistribute the data:

- **Flights** — the [OpenFlights](https://openflights.org/data.html) airport and
  route databases, under the
  [Open Database License (ODbL)](https://opendatacommons.org/licenses/odbl/1-0/).
- **Ground transit** — GTFS schedule feeds discovered through the
  [Mobility Database](https://mobilitydatabase.org/) catalog (catalog metadata
  under CC0); each feed is under its own license, set by the publishing agency.
  For countries whose catalog entries lack a national long-distance rail feed, a
  curated publisher feed is fetched too — currently [gtfs.de](https://gtfs.de/)
  (German long-distance and regional rail, DELFI data) — credited the same way.
- **Driving** — street routing uses
  [OpenStreetMap](https://www.openstreetmap.org/copyright) extracts (fetched via
  [Geofabrik](https://download.geofabrik.de/)) under the ODbL; credit
  "OpenStreetMap contributors".

No data is bundled in this repository; everything is downloaded and cached at
runtime. Run `travelplanner attribution` for the notice, or `travelplanner
attribution ORIGIN DEST` to list the GTFS feeds (and license URLs) covering a
trip (the planner uses the smallest-area one).

## License

MIT — see [LICENSE](LICENSE).
