# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and this project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
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

### Changed
- `drive()` reuses a cached customized road metric (`CCHRoadRouter.customized`)
  instead of rebuilding it per call, ~5x batch throughput; `customize()` still
  returns a fresh, mutable metric.
- `Location` validates lat/lon range, so invalid coordinates raise instead of
  silently producing an empty `plan()` result (empty now means "no route", not
  "bad input").

### Fixed
- `road_router` lru_cache key normalized: `road_router(region)` and
  `road_router(region, None)` no longer build the region twice.

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
