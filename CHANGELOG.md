# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and this project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- A preferred way of transportation, replacing the car-first/fly-first default that
  read as US-centric. A named preference -- `transit` (the new default: walk to
  stops, no car to the station), `train` (also suppresses flights on rail-doable
  corridors, with the flight returning only when there is no same-day train), `drive`,
  or `fastest` -- maps to a coherent set of planner arguments (`TRANSPORT_PREFERENCES`
  / `preference_kwargs`). The planner gains `exclude_modes` (drop candidates using a
  mode unless that leaves nothing, or only options slower than a same-day rail trip),
  which is how "trains, not planes" suppresses flights rather than merely ranking them
  down. The demo offers the preference as a sticky setting (it remembers your choice)
  and now shows several itineraries at once, each labelled by purpose
  (Fastest / Cheapest / Greenest / Fewest changes) and re-sortable -- the transit-first
  preferences lead with the rail option when a same-day train exists (the faster
  flight or drive stays, just behind it). Built on the new
  `plan_trip_choices` / `plan_labeled` -- one best option per objective, deduped so a
  trip that wins several keeps all its labels, generated from one candidate pool so the
  labelled view costs about one ordinary plan.
- Demo usability: a row of selectable example trips (e.g. Amsterdam to Berlin by
  train, London to New York, Madrid to Santorini) that fill the origin/destination,
  preference, and rail toggle in one click (`/api/examples`); flight legs drawn as
  true great-circle arcs, so a long-haul flight curves the way it actually flies
  instead of a straight Mercator line (date-line safe); and a mode-colour map legend.
- Ground transit in door-to-door planning, end to end. The GTFS loader honours
  station/platform hierarchy (`parent_station`): a trip departs a platform, but a
  coordinate snaps to the station, so the loader links the two with a short footpath
  (and the scan's footpath closure reaches sibling platforms) -- without it a snapped
  station carried no departures and rail silently returned nothing. Feed selection is
  robust to the Mobility Database's dead links and to ranking by bounding box: a
  covering feed that fails to download or carries no corridor service is skipped, and
  rather than merging only the single smallest-box feed (which can be a sparse
  long-distance operator that lacks the real through-train), several covering feeds
  are merged up to a trip-count budget -- so the dense national feed carrying the
  service is included (e.g. Amsterdam->Berlin now surfaces the ~8h IC instead of a
  garbage 46h chain), bounded so the merged scan stays fast. The demo web service
  gains a "trains & buses" toggle that
  auto-composes a trip-scoped timetable (flights + covering GTFS feeds) per request
  and surfaces the coverage notes. Whether a train tops the list still depends on the
  objective -- door-to-door a short hop is often faster by car (try `greenest`).
- Approximate fare estimates. A pluggable, always-on fare model
  (`travelplanner.fares`) prices each leg from a distance-and-mode heuristic, so
  every `Itinerary`/`Leg` carries `fare_estimate`/`fare_currency` (in `to_dict()`
  and on the CLI route card), and CHEAPEST ranks on the continuous amount instead
  of the 3-level cost band -- separating same-band options (two flights, or a long
  train vs a short drive) that previously tied. It is an estimate for ranking and a
  ballpark, **not a quoted fare** (it ignores discounts, daily caps, transfer rules,
  and advance-purchase pricing); swap a model or opt out with `set_fare_model` /
  `free_model`. The `cost_level` band is unchanged (still mode-based, for display).
- Time-of-day driving in door-to-door planning. `plan_trip(..., road=True)` now
  threads the departure through the road speed model, so access/egress car legs
  reflect weekday rush-hour and night congestion (average-congestion model by
  default, free-flow opt-in) instead of a single average. The `RoadConnector`
  `access`/`egress`/`direct` methods take a `depart_at` (the standalone `drive*`
  APIs already did); egress references departure-time congestion, since its arrival
  time is unknown when legs are priced up front. The connector now reuses the
  router's customized-metric cache (keyed by day, conditions, model, and hour).
- Routed driving geometry on the result. A road-backed car `Leg` carries
  `geometry`, the routed polyline as `(lat, lon)` points along the real street
  network, and `leg.to_dict()` emits it as `[[lat, lon], ...]`; walk, straight-line,
  and transit legs leave it `None` (their path is just `from_loc -> to_loc`).
- Route-card-friendly results. Each `Leg` now carries absolute `depart_at`/
  `arrive_at` (stamped from the itinerary's departure, local to each endpoint via
  its `tz`) and a `describe()` step summary ("Flight from Schiphol to Zurich
  Airport"). `humanize_duration` plus `Itinerary.total_duration_human` /
  `Leg.duration_human` format a length as "2h 9m". `to_dict()` exposes all of these
  (`summary`, per-leg `depart_at`/`arrive_at`, `*_human`) with clock times rounded
  to whole seconds, so a consumer renders a Google-Maps-style card without
  re-deriving the running clock. The CLI prints the humanized total and gains a
  `plan --top N` flag. `CostLevel` is documented as a relative band (no fare model).
- Auto-sourced timetables (`plan_trip` with no `Timetable`, and the default for
  `travelplanner plan`): omit the feed and one is composed for the trip -- the
  OpenFlights flight network scoped to airports near the endpoints, plus the GTFS
  feed(s) whose bounding box covers the route, selected from the Mobility Database
  catalog (`transit_catalog`), downloaded and cached, then clipped to the trip
  corridor and merged (`auto_timetable.build_default_timetable`,
  `merge_timetables`, `clip_timetable`). Coverage gaps surface as warnings; GTFS
  coverage is uneven by region and flight times are synthetic, so supply a feed
  for exact data.
- Data attribution for the fetched datasets (`attribution` module and the
  `travelplanner attribution [ORIGIN DEST]` command): a single source of truth for
  crediting OpenFlights (ODbL), the Mobility Database GTFS feeds (each under its
  own license), and OpenStreetMap road extracts via Geofabrik (ODbL). The catalog
  now records each feed's license URL (`Feed.license_url`), so `attribution ORIGIN
  DEST` credits the one feed the planner actually fetches (smallest covering box)
  and lists the rest as unused. Credit is use-accurate: the auto `plan` footer
  credits only the datasets its result legs drew on, and `drive` credits
  OpenStreetMap. README and `docs/how-it-works.md` gain a "Data sources and
  licensing" section.
- Timezone-aware connections: stops carry an IANA timezone (`Stop.tz`, read from
  GTFS `agency.txt`/`stop_timezone` and the OpenFlights tz column, validated at
  load). A feed with timezone data materializes connections in absolute UTC via
  the stdlib `zoneinfo`, so the scan runs in one frame and international trips are
  timed correctly across zones; a naive departure is read as local at the origin.
  A feed with no timezone data stays naive, so single-zone behavior is unchanged.
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
- GTFS `frequencies.txt` is expanded into concrete runs: a headway-based trip
  (departs every N seconds between a start and end time) becomes individual
  departures shifted across the window, so frequency-defined service is scanned
  like any other. Implausibly dense windows are skipped to bound the expansion.
- Inter-feed transfers in the auto-sourced network: when a flight (or ferry) feed
  is joined to ground transit, footpaths now link each airport/ferry terminal to
  the nearby ground stops (`link_transfer_hubs`), so a trip can chain a train to
  the airport with a flight instead of leaving the two networks disconnected.
- GTFS `transfers.txt` is now honoured. Same-stop rows set a stop's minimum change
  time (type 1 timed -> no buffer, type 2 -> its minimum, type 3 -> transfers not
  possible there, modelled as `Stop.min_transfer=None` so the scan never changes
  vehicles at that stop while still allowing boarding); inter-stop rows become
  footpaths (the stated minimum time, or the walking time between the stops when
  none is given; type 3 adds none). `Timetable.transfer_time` may now return None.
- Connecting flights in the auto-sourced network. The flight network now adds the
  busiest hub airports near the trip (`openflights.hub_airports`) as connection
  points, so a trip with no direct flight routes origin -> hub -> destination (e.g.
  Austin -> Atlanta -> Zurich). The hub set is capped to the few busiest hubs in
  range, so the synthetic-flight count and scan time stay bounded (measured ~1.2k
  flights / ~10 ms for a transatlantic trip).
- Offline timetable artifact: `travelplanner transit-build ORIGIN DEST out.json`
  composes a trip's timetable (flights + covering GTFS) once and serializes it
  (`save_timetable` / `load_timetable_artifact`, a compact JSON file), and `plan
  --timetable out.json` loads it to route fully offline -- no catalog, feed, or
  flight download at plan time. Durations are stored as float seconds so the
  timetable round-trips exactly; the validity JSON helpers moved to `graph.validity`
  and are now shared by the road and timetable artifacts.

### Changed
- `plan_trip(origin, dest)` is now the minimal call: `depart_at` is optional
  (defaults to now) and the default `objective` is `FASTEST` (was `AIR_PRIORITY`,
  a surprising default for a neutral "best routes" call). Air priority is still
  available via `objective=AIR_PRIORITY`; the CLI `plan --objective` default is
  `fastest` too, and the lower-level `plan()`, `plan_multi()`, and `TravelQuery`
  now default to `FASTEST` as well so every entry point agrees.
- No more optional extras: the road engine (`routingkit-cch`, `osmium`) and the
  calendar package (`holidays`) are now core dependencies, so one install
  gets every feature and `pytest` runs the whole suite with nothing
  skipped. The `road` and `calendar` extras are removed; only a `dev` extra
  (pytest) remains. Installing now needs a C++17 compiler with OpenMP (for the
  source-built road engine). Tests no longer gate on `pytest.importorskip`, and CI
  fails if any test is skipped.
- `drive()` reuses a cached customized road metric (`CCHRoadRouter.customized`)
  instead of rebuilding it per call, ~5x batch throughput; `customize()` still
  returns a fresh, mutable metric.
- Road graph columns are packed into narrower dtypes: float32 coordinates (~0.6 m
  precision, ample for snapping) and 16-bit interned validity/class indices,
  cutting the per-node coordinate memory in half and trimming each arc -- about
  800 MB off a 50M-node / 100M-arc country graph. The offline artifact stores the
  same narrow dtypes (format version 4; rebuild older artifacts).
- `Location` validates lat/lon range, so invalid coordinates raise instead of
  silently producing an empty `plan()` result (empty now means "no route", not
  "bad input").
- Auto-sourced data now refreshes on a max-age TTL instead of caching forever: the
  Mobility Database catalog and GTFS feeds after 7 days, OpenFlights after 30
  (`CATALOG_MAX_AGE`/`FEED_MAX_AGE`/`OPENFLIGHTS_MAX_AGE`; a stale feed zip is
  re-extracted). The refresh is offline-first via `roads.refresh_if_stale`: if a
  refresh fails but a cached copy exists, that copy is used with a warning rather
  than failing, so a network blip never breaks an otherwise-usable cache.

### Fixed
- Route-quality fixes from a multi-city logic check (10 city pairs, three personas).
  (1) Implausible transit journeys are dropped before ranking: a vehicle leg that rode
  far around for its endpoints, a route whose legs sum to a gross multiple of the
  straight-line trip (e.g. Vienna->Venice routed via Stuttgart, 24h), or an
  excessive-leg chain stitched from sparse feeds -- none reach the result. (2) GREENEST
  now ranks on the modelled emissions estimate first, not minimised car-distance, so a
  short-access flight is never returned as "greenest" over driving or rail (a 230 km
  Rome->Florence flight was). (3) The demo notes a likely rail-coverage gap ("no train
  in these results -- there may be rail we have no data for") when an auto-composed trip
  over a rail-plausible distance returns none, so an absent train reads as missing data,
  not a verdict.
- Footpath transitive closure no longer hangs on a dense feed. It was an all-pairs
  Floyd-Warshall (O(V^3)) assuming a small footpath graph, but a city-scale GTFS feed
  has tens of thousands of footpath nodes, so building the scan hung. It is now a
  bounded per-source Dijkstra over the (sparse) footpath graph -- dropping only
  unrealistically long composed walking chains while always honouring direct
  feed-supplied transfers -- so a dense metro plans in seconds instead of hanging.
  Connections are also materialized once per scan window (memoized) instead of
  rebuilt for each line-haul mode set and egress query, and a run whose first
  departure is past the window is skipped without materializing its segments.
- Geofabrik geometry catalog: `refresh=True` now re-downloads the index on every
  call instead of being memoized by an `lru_cache` keyed on the flag (which had
  re-fetched only the first time and then returned the stale cached copy). The
  parsed catalog is still cached for the common no-refresh path.
- GTFS loader robustness: a non-timepoint stop with empty `arrival_time`/
  `departure_time` (valid per the spec) no longer crashes the whole feed load --
  the untimed stop is dropped and the trip keeps its timed stops. A trip whose
  `service_id` is not defined in `calendar`/`calendar_dates` is now dropped
  instead of being treated as active every day (which had created phantom trips);
  a feed with no calendar files at all still falls back to always-active.
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
