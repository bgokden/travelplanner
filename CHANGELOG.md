# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and this project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed
- The heuristic estimator no longer proposes ground (car/train) itineraries for
  trips beyond a plausible ground range (e.g. a "train across the ocean" for
  New York -> Tokyo). Car and train now have an upper distance bound, enforced
  alongside the existing lower bounds. The estimator remains distance-bounded,
  not land-route-aware (use the multimodal engine for real routing).

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
