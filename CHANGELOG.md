# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and this project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- OpenFlights flight data (`load_openflights`): build a Timetable from the open
  OpenFlights airport + route dataset. Airports become AIRPORT stops and each
  directed non-stop route becomes synthetic daily flights whose duration is
  estimated from great-circle distance and a cruise speed (OpenFlights has no
  real schedules, so the times are synthetic but the airports and route network
  are real). Reads local `airports.dat`/`routes.dat` or fetches+caches them with
  `download=True`; `keep={IATA, ...}` restricts to a manageable subnetwork. Fills
  the "no bundled flight schedules" gap so the air line-haul can route over real
  airports.
- Sample-trip map view: render a door-to-door `plan_trip` itinerary as a
  coloured overlay on a self-contained Leaflet map. New generic
  `viz.segments_map_html`/`save_segments_map` (coloured polylines + legend) that
  the itinerary and route maps now share, and a `geometries=` override on
  `itinerary_map_html`/`save_itinerary_map` mapping a 1-based leg index to its
  real routed `(lat, lon)` path -- so road legs follow the streets (e.g. from
  `drive_route`) while `viz` stays road-engine-free; legs without geometry stay
  straight. Fixed the door-to-door Destination marker (now the last leg's
  endpoint, not the first leg's) and made `routes_map_html` ride on the shared
  renderer. Runnable `examples/trip_map.py` builds and writes a sample map.
- Asymmetric first/last mile (`plan_trip(..., access="transit", egress="car")`):
  `egress` overrides the last-mile mode independently of `access` (default: same
  as access), built as a `SplitConnector` delegating each end to its own mode
  connector -- e.g. take the train to the airport, then a rental car from the
  arrival airport to the door. `egress` is "car" or "transit"; it cannot combine
  with `access="both"` or with `road=True` (those raise). The connector-selection
  branching is consolidated into `_validate_modes` + `_select_connectors` and the
  whole path now goes through `plan_multi`. The pure-ground (no-transit) candidate
  follows the first-mile mode, so a short door-to-door hop is walked/driven
  consistently whether or not `egress` differs from `access`.
- Access-mode diversification (`plan_trip(..., access="both")`) and the
  underlying `plan_multi(origin, dest, depart_at, timetable, connectors, ...)`:
  pool door-to-door candidates from several connectors before a single Pareto/
  ranking pass, so a drive-to-airport itinerary and a walk-to-train one compete
  on one frontier. This surfaces the transit-access option that a single
  earliest-arrival CSA run would otherwise never generate -- so `GREENEST` now
  leads with walk -> train -> flight while `AIR_PRIORITY` leads with the drive,
  in the same call. (`access="both"` uses geometric connectors; for road-backed
  car access, build connectors and pass `connector=`.)
- `Objective.GREENEST`: ranks itineraries by least private-car distance, then
  time. Private-car distance is now a fourth Pareto criterion alongside (time,
  cost, transfers), so a low-driving option (e.g. walk -> train -> flight) stays
  on the frontier instead of being pruned by a faster drive-to-airport flight
  with fewer transfers; GREENEST then surfaces it first while the other
  objectives are unchanged. Available everywhere an `Objective` is (`plan`,
  `plan_trip`, CLI `--objective greenest`). It ranks across the options already on
  the frontier; to compare car vs transit *access* to the same flight, pair it
  with `access="transit"`.
- Door-to-door multimodal trip planning (`plan_trip`): give two locations (name,
  "lat,lon", tuple, or Location), a departure time, and a GTFS `Timetable`, and
  get ranked door-to-door itineraries (ground access -> rail/ferry/flight
  line-haul -> egress) over the Pareto frontier for an `Objective`. It is glue
  over the existing engine: it geocodes the endpoints and picks a connector, so
  the caller no longer hand-builds one. Default access/egress is the region-free
  `GeometricConnector`; `road=True` upgrades to a road-network connector when one
  Geofabrik region covers both endpoints (and only the nearby stops are snapped,
  not the whole feed), falling back to geometric for cross-border trips rather
  than loading a country-scale extract. An explicit `connector=` overrides the
  choice. `turn_aware=True` (with `road=True`) backs the road connector with the
  edge-expanded, turn-correct router, so the driving legs (access/egress and the
  direct-drive candidate) honour turn restrictions and junction/signal costs
  (validated Zaandam->Amsterdam: the direct drive 20.2 -> 24.0 min, +19%, vs the
  node-based estimate). It needs a `data_dir` built with
  `build_region(..., turn_aware=True)` or an online parse.
- `region_connector(..., turn_aware=True)` and a key-based
  `ExpandedCustomized.route(from_key, to_key)` (mirroring the node-based
  `CustomizedRoad.route`), so a `CCHConnector` can be backed by the turn-aware
  router and stays router-agnostic.
- `SplitConnector(access_connector, egress_connector, *, direct_connector=None)`:
  a composite RoadConnector that resolves access in the origin's region and
  egress in the destination's region, for door-to-door trips whose endpoints fall
  in different road extracts (e.g. Zaandam -> Maastricht). `plan_trip(road=True)`
  uses it automatically when no single region covers both endpoints and the trip
  is online (a single `data_dir` cannot hold two regions, so an offline
  cross-region trip falls back to geometric); each side is pre-filtered to its
  endpoint's nearby stops and honours `turn_aware`. The cross-region pure-ground
  drive spans no single graph, so `direct` is delegated to a geometric estimate.
- Transit access mode (`plan_trip(..., access="transit")`): selects the
  first/last-mile mode like a "Driving" vs "Transit" tab. The default `"car"`
  drives/walks to the nearest stop; `"transit"` only walks to a stop within a
  short radius, so longer access hops (e.g. the train to the airport) are taken
  via the scheduled network instead of driving. This surfaces walk -> train ->
  flight itineraries that the car-access default hides: driving straight to the
  airport reaches the same flight with fewer transfers and so dominates the
  train-access variant on the Pareto frontier. With `"transit"` there are no car
  legs, so `road`/`turn_aware` do not apply (passing both raises).
- Multimodal itinerary map (`viz.itinerary_map_html` / `save_itinerary_map`):
  render a `plan_trip` itinerary's legs as per-mode coloured segments (walk grey,
  car blue, train green, ferry teal, flight orange) on one self-contained Leaflet
  map, with a per-leg legend.
- Turn-aware driving (`drive(..., turn_aware=True)`): routes over an
  edge-expanded graph (nodes = road arcs, edges = turns) so turn restrictions and
  turn/junction costs are modelled like a production router. OSM via-node turn
  restrictions (`no_*`/`only_*`) are parsed and honoured (legal routes); geometric
  turn costs (left/right/straight/sharp/U-turn, OSRM-style, mirrored for left-hand
  traffic) apply only at real junctions, plus a surcharge at
  `highway=traffic_signals` nodes. Validated urban (Amsterdam centre +38%, in
  Google's range; restrictions reroute Zaandam->Schiphol to a legal path). The
  node-based engine remains the default; turn-aware is opt-in and heavier (it
  parses signal + restriction data, so its OSM load is slower). Offline:
  `build_region(region, out_dir, turn_aware=True)` persists signals, restrictions
  and the turn-expanded contraction order (artifact format v3), so
  `drive(..., turn_aware=True, data_dir=...)` loads with no network or re-expand.
- Offline road artifacts (`build_region`, `travelplanner build`): parse the OSM
  extract and compute the CCH contraction order at build time, write them to an
  explicit directory, and load them at runtime with no network and no re-parsing
  (`drive(..., data_dir=...)`, `road_router(region, data_dir)`). Rebuilding the
  CCH from the saved order is near-instant, so cold start drops from minutes to
  seconds at country scale.
- `NodeGrid` uniform-grid spatial index for nearest-road-node snapping, replacing
  the O(n) linear scan in `drive`/snapping and the `CCHConnector`.
- `drive_matrix(points, region, ...)`: batch driving over all origin x dest pairs,
  reusing one customized metric and snapping each point once.
- Serialization/tabular helpers: `to_dict()`/`to_json()` on `Itinerary`/`Leg`/
  `Location`/`DriveResult`, plus `itinerary_records`/`leg_records` for pandas;
  `Itinerary.num_transfers` and `total_minutes`.
- `bench/api_smoke.py`: self-contained smoke + latency-budget check for CI.
- Automatic region selection: `drive`/`drive_matrix` no longer require a
  `region` -- when omitted it is auto-selected as the smallest Geofabrik extract
  whose polygon covers all endpoints (via the geometry index, `region_for` /
  `region_for_trip`). A trip no single extract covers (cross-border or across
  water, e.g. Amsterdam->London) raises a clear error pointing to plan().
- Dynamic speed models (`travelplanner.speed`): driving times now use a
  configurable speed model instead of raw free-flow speed limits. A model maps
  `(highway_class, depart_at) -> time multiplier`; `average_model` (the new
  default) reflects typical conditions, `time_of_day_model` adds a rush-hour /
  weekday congestion curve, and `free_flow_model` is the opt-in best case.
  `set_speed_model`/`reset_speed_model` set the active default; `drive`/
  `drive_matrix` take `depart_at=` and `speed_model=`. Applied at customization
  per interned highway class, so one artifact serves any profile/time with no
  rebuild. (Heuristic typical-day, not live traffic; pluggable for real data.)
- Calendar-aware speed (`holiday_calendar`, via the holidays package): a public
  holiday collapses the rush-hour peak and a school
  holiday lightens it. School breaks come from explicit ranges, or automatically
  from the holidays package's SCHOOL category where it has data (e.g. Germany per
  Bundesland; coverage elsewhere is sparse, so supply ranges there).
- Pluggable geocoding (`travelplanner.geocoding`): a geocoder is a callable
  `(name) -> (lat, lon) | None`; compose with `chain`, `cached` (JSON disk cache),
  and an opt-in online `nominatim_geocoder`. `set_geocoder`/`reset_geocoder` set
  the active one (default: bundled table, offline); `city()`/`drive()`/
  `drive_matrix()` take a per-call `geocoder=`. Pre-warm the cache at build time to
  resolve names offline at runtime.

### Changed
- No more optional extras: the road engine (`routingkit-cch`, `osmium`) and the
  calendar package (`holidays`) are now core dependencies, so `pip install
  travelplanner` gets every feature and `pytest` runs the whole suite with nothing
  skipped. The `road` and `calendar` extras are removed; only a `dev` extra
  (pytest) remains. Installing now needs a C++17 compiler with OpenMP (for the
  source-built road engine). Tests no longer gate on `pytest.importorskip`, and CI
  fails if any test is skipped.
- `drive()` reuses a cached customized road metric (`CCHRoadRouter.customized`)
  instead of rebuilding it per call, ~5x batch throughput; `customize()` still
  returns a fresh, mutable metric.
- `Location` validates lat/lon range, so invalid coordinates raise instead of
  silently producing an empty `plan()` result (empty now means "no route", not
  "bad input").

### Fixed
- CSA no longer returns journeys with infeasible transfers. The scan now tracks
  each run's boarding connection, so when a faster run improves an interior stop
  of a ride-through, journey reconstruction still rides the boarded run from where
  it was actually boarded instead of stitching an unchecked (possibly too-short)
  transfer at that stop.
- `Timetable.transfer_time` returns a 5-minute default for a stop a trip passes
  through but that was never registered, instead of zero (which had allowed an
  impossible same-instant vehicle-to-vehicle change); `ConnectionScan.arrival_times`
  returns an empty dict for empty sources instead of crashing.
- Planner ranking: `plan`/`plan_multi` no longer drop a cheaper or lower-driving
  itinerary before the Pareto stage -- de-duplication now keys on all four axes
  (time, cost, transfers, private-car distance) plus the mode sequence, not just
  duration and modes. `AIR_PRIORITY` prefers an itinerary with an actual flight
  leg rather than one whose longest leg is a flight, so a flight reached by a long
  airport drive is still prioritized.
- `road_router` lru_cache key normalized: `road_router(region)` and
  `road_router(region, None)` no longer build the region twice.
- `CCHConnector` walks short hops: a stop (or a direct trip) within
  `walk_threshold_km` is now a WALK leg instead of always a CAR leg, matching
  `GeometricConnector`. This also fixes an unrealistic 0-second egress when the
  destination snapped to the same road node as the alighting stop (a free
  teleport); the final sub-km hop to the door is now a short walk.
- `CCHConnector.access`/`egress`/`direct` no longer crash on `day=None` (allowed
  by the `RoadConnector` protocol) over a seasonally-validated graph; they
  default to the current date, matching `drive_route`.

### Changed (memory)
- Road graph node keys: integer (OSM) ids now pack into a compact `array("q")`
  instead of a `list[str]`, and the reverse key->index map is built lazily, so
  index-based routing (`route_index`, used by `drive`) never materializes it.
  Together these cut the per-process node-key footprint by ~10x at country scale
  (the lever for running many restarting workers offline). Arbitrary string keys
  still work unchanged.

### Removed
- The non-graph heuristic estimator (`estimate`, `PlannerConfig`, `ModeProfile`,
  the bundled airport table). It computed itineraries from straight-line
  distance with no land-route awareness, so it could propose meaningless routes
  (e.g. a "train across the ocean"). The graph engine `plan` is now the only
  planner: it traverses only real edges, so it cannot suggest a route that does
  not exist. The CLI gains a `plan` command and drops `estimate`.

## [0.1.0] - 2026-06-19

Initial release.

### Added
- Multimodal door-to-door planner (`plan`): ground access + scheduled line-haul
  (rail/ferry/flight) + egress, with multi-criteria Pareto selection over
  (time, cost, transfers) and air prioritized.
- Road layer: Customizable Contraction Hierarchies engine (`graph.road`) over
  OpenStreetMap, with seasonal/conditional edge validity; optional `road` extra.
- Scheduled layer: Connection Scan Algorithm (`graph.scheduled`) with a GTFS
  loader; pure standard library.
- Coupling layer (`graph.coupling`): geometric and CCH road connectors.
- Heuristic estimator (`estimate`) with bundled airport/city tables, no deps.
- `Validity` model (GTFS-style calendars, recurring open months, condition
  flags) shared by road and scheduled layers.
- CLI: `travelplanner demo` and `travelplanner estimate`.
- Bundled sample data (`sample_timetable`, `sample_trip`).

### Known limitations
- Naive local times (no timezone handling for international flights).
- Country-scale road graphs are memory-heavy (node bookkeeping).
