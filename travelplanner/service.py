"""Demo HTTP service: a small JSON API + a Leaflet map UI for trip planning.

Pure stdlib (http.server) -- no extra dependencies. Start it with:

    python -m travelplanner.service               # http://127.0.0.1:8000
    python -m travelplanner.service --region switzerland   # road-backed car legs

Endpoints:
    GET /                  the map UI (enter origin/dest, see ranked trips)
    GET /api/plan?origin=&dest=&depart=&prefer=&top=&road=&transit=&region=
                           itineraries labelled by purpose (Fastest/Cheapest/
                           Greenest/Fewest changes) for the chosen transport
                           preference, each with per-leg map segments and labels
    GET /api/example       a ready-made origin/dest/depart for the bundled feed
    GET /api/health        {"status": "ok"}

The default plans over the bundled sample timetable with straight-line legs, so
the demo runs offline with no downloads. Pass a `region` (per request or via
--region) to follow real streets on car legs (downloads/builds that extract).
"""

import argparse
import json
import os
import time
import warnings as warnings_mod
from collections import OrderedDict
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from travelplanner.catalog import search_cities
from travelplanner.geo import haversine
from travelplanner.geocoding import (bundled_geocoder, cached, chain,
                                     nominatim_geocoder, nominatim_search)
from travelplanner.models import LINE_HAUL_MODES, Mode
from travelplanner.graph.query import Objective
from travelplanner.graph.schema import NodeType
from travelplanner.openflights import (load_airports, load_flight_network,
                                       search_airports)
from travelplanner.roads import _coerce
from travelplanner.samples import sample_timetable
from travelplanner.trips import (DEFAULT_TRANSPORT_PREFERENCE,
                                 plan_trip_choices, preference_kwargs)
from travelplanner.viz import MODE_COLORS, itinerary_segments

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000

# How many planned responses to keep in the per-server LRU cache.
_PLAN_CACHE_MAX = 128
# Identify the demo to Nominatim (required by their usage policy).
USER_AGENT = "travelplanner-demo/1.0 (+https://github.com/bgokden/travelplanner)"
# Nominatim asks for at most ~1 request/second; throttle network suggestions.
_NOMINATIM_MIN_INTERVAL = 1.0
# An OSM hit that shares a name with an already-offered place AND is within this
# distance of it is the same place (their centroids differ slightly), so it is
# dropped as a duplicate. Generous on purpose: the name match -- not distance --
# discriminates the same place from a distinct nearby one (Vatican vs Rome) or a
# distant namesake (Paris, Texas).
_SUGGEST_DEDUP_KM = 50.0


def _build_geocoder(online: bool, user_agent: str):
    """Single-result geocoder for planning: bundled, plus cached Nominatim online."""
    if not online:
        return bundled_geocoder
    return chain(bundled_geocoder, cached(nominatim_geocoder(user_agent=user_agent)))


# "Load example" trips: London->New York exercises the real flight network; the
# sample feed can only route its own canonical trip (see samples.sample_trip).
_CITY_EXAMPLE = {"origin": "London", "dest": "New York"}
_SAMPLE_EXAMPLE = {"origin": "47.00,7.005", "dest": "45.00,9.01"}
# Above this stop count a feed is the real flight network, not the tiny sample.
_FLIGHT_NETWORK_MIN_STOPS = 50

# Curated, selectable example trips for the demo, by feed source. The flight feed
# (online) routes real city pairs; the rail ones set the trains & buses toggle so a
# trip-scoped timetable is auto-composed. The sample feed (offline) only routes its
# own canonical coords, but it carries both a flight and a train, so it can still show
# the preset difference. Each entry is a label plus the form fields a click fills in.
_FLIGHT_EXAMPLES = [
    {"label": "Amsterdam to Berlin by train", "origin": "Amsterdam", "dest": "Berlin",
     "prefer": "train", "transit": True},
    {"label": "Munich to Salzburg by transit", "origin": "Munich", "dest": "Salzburg",
     "prefer": "transit", "transit": True},
    {"label": "London to New York", "origin": "London", "dest": "New York",
     "prefer": "fastest", "transit": False},
    {"label": "Madrid to Santorini", "origin": "Madrid", "dest": "Santorini",
     "prefer": "fastest", "transit": False},
]
_SAMPLE_EXAMPLES = [
    {"label": "Sample: fastest", "origin": "47.00,7.005", "dest": "45.00,9.01",
     "prefer": "fastest", "transit": False},
    {"label": "Sample: by train", "origin": "47.00,7.005", "dest": "45.00,9.01",
     "prefer": "train", "transit": False},
]


def _default_timetable(online: bool):
    """The demo's default feed: the real OpenFlights flight network (so air
    priority finds real flights), falling back to the bundled sample when the
    data is missing, unreachable, OR yields an empty network (e.g. an over-tight
    route filter). Returns (timetable, source) with source "flights" or "sample"
    so the caller can offer a matching example trip."""
    try:
        tt = load_flight_network(download=online)
        if tt.stops:
            return tt, "flights"
    except (FileNotFoundError, ValueError, OSError):
        pass
    return sample_timetable(), "sample"


def _search_stops(timetable, query: str, limit: int) -> list:
    """Transit stops (rail/ferry, not airports) from the feed matching `query`.

    Airports are offered separately from the OpenFlights index, so they are
    excluded here. Name prefix matches rank before mere substring matches.
    """
    q = query.strip().lower()
    if len(q) < 2:
        return []
    hits = [s for s in timetable.stops.values()
            if s.type is not NodeType.AIRPORT and q in (s.name or "").lower()]
    hits.sort(key=lambda s: (not (s.name or "").lower().startswith(q),
                             (s.name or "").lower()))
    return hits[:limit]


def _name_key(label: str) -> str:
    """First component of a place label, lowercased: bundled 'Paris, France' and
    the OSM 'Paris, Ile-de-France, France' both reduce to 'paris'."""
    return label.split(",")[0].strip().lower()


def _geocode_suggestions(server, query: str, limit: int) -> list[dict]:
    """Autocomplete candidates: bundled cities first, then OSM (online, throttled).

    Returns [{"label", "lat", "lon", "source"}]. Bundled hits are instant and
    offline; Nominatim is consulted only when the server is online, capped at one
    request per `_NOMINATIM_MIN_INTERVAL` seconds, and results are memoised per
    query so repeated keystrokes never re-hit the network.
    """
    q = query.strip()
    if len(q) < 2:
        return []
    cache = server.geo_cache
    key = (q.lower(), limit)
    if key in cache:
        return cache[key]
    out: list = []
    seen: set = set()        # rounded coords (~110 m): exact-dup across all sources
    places: list = []        # (name_key, lat, lon) of offered places; an OSM hit is
                             # a duplicate only if it shares a name AND is near one.
                             # Proximity alone drops distinct nearby places (Vatican
                             # vs Rome); a name alone drops a distant namesake.
    for row in search_cities(q, limit=limit):
        label = ", ".join(p for p in (row["name"], row["country"]) if p)
        out.append({"label": label, "lat": row["lat"], "lon": row["lon"],
                    "source": "city"})
        seen.add((round(row["lat"], 3), round(row["lon"], 3)))
        places.append((_name_key(row["name"]), row["lat"], row["lon"]))
    for air in search_airports(q, limit=limit, airports=server.airports):
        if len(out) >= limit:
            break
        ck = (round(air["lat"], 3), round(air["lon"], 3))
        if ck in seen:
            continue
        seen.add(ck)
        label = f"{air['name']} ({air['iata']})"
        if air["country"]:
            label += f", {air['country']}"
        out.append({"label": label, "lat": air["lat"], "lon": air["lon"],
                    "source": "airport"})
        places.append((_name_key(air["name"]), air["lat"], air["lon"]))
    for stop in _search_stops(server.timetable, q, limit):
        if len(out) >= limit:
            break
        ck = (round(stop.lat, 3), round(stop.lon, 3))
        if ck in seen:
            continue
        seen.add(ck)
        out.append({"label": stop.name, "lat": stop.lat, "lon": stop.lon,
                    "source": "station"})
        places.append((_name_key(stop.name), stop.lat, stop.lon))
    cacheable = True
    if server.online and len(out) < limit:
        now = time.monotonic()
        if now - server.last_nominatim >= _NOMINATIM_MIN_INTERVAL:
            server.last_nominatim = now
            for r in nominatim_search(q, user_agent=server.user_agent, limit=limit):
                ck = (round(r["lat"], 3), round(r["lon"], 3))
                nkey = _name_key(r["name"])
                if ck in seen or any(
                        nkey == pkey
                        and haversine(r["lat"], r["lon"], plat, plon) <= _SUGGEST_DEDUP_KM
                        for pkey, plat, plon in places):
                    continue
                seen.add(ck)
                places.append((nkey, r["lat"], r["lon"]))
                out.append({"label": r["name"], "lat": r["lat"], "lon": r["lon"],
                            "source": "osm"})
                if len(out) >= limit:
                    break
        else:
            cacheable = False        # throttled now; let a later query try OSM
    if cacheable:
        cache[key] = out
    return out

_DEPART_FORMATS = ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M",
                   "%Y-%m-%d %H:%M", "%Y-%m-%d")


def _parse_depart(value, default: datetime) -> datetime:
    """Parse a depart timestamp (several common forms), or fall back to default."""
    if not value:
        return default
    for fmt in _DEPART_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    raise ValueError(f"could not parse depart time {value!r} (use YYYY-MM-DDTHH:MM)")


# Distance band over which an intercity train would normally exist; if the result
# shows none across it, that is likely a GTFS coverage gap (the feed lacks the line),
# not "there is no train" -- so the UI says so rather than presenting a flight/drive as
# the verdict. Below the band trips are local; above it flights legitimately dominate.
_RAIL_PLAUSIBLE_MIN_KM = 30.0
_RAIL_PLAUSIBLE_MAX_KM = 1000.0

# The purposes the demo offers as labelled, re-sortable choices: the single best
# itinerary per objective, deduped (a trip that wins several keeps all its labels).
# Ordered so the default lead is the fastest option; the user can re-sort by label.
# air_priority is intentionally absent -- a "purpose" is what a traveller asks for,
# and FASTEST already surfaces a flight when flying is genuinely fastest.
_CHOICE_OBJECTIVES = (
    (Objective.FASTEST, "Fastest"),
    (Objective.CHEAPEST, "Cheapest"),
    (Objective.GREENEST, "Greenest"),
    (Objective.FEWEST_TRANSFERS, "Fewest changes"),
    (Objective.MOST_DIRECT, "Most direct"),
)


def plan_response(origin, dest, depart_at: datetime, timetable=None, *,
                  prefer: str = DEFAULT_TRANSPORT_PREFERENCE, top_n: int = 4,
                  road: bool = False,
                  region: str | None = None, data_dir: str | None = None,
                  turn_aware: bool = False, geocoder=None,
                  online: bool = True) -> dict:
    """Plan a door-to-door trip and shape it for the map UI (JSON-safe dict).

    `prefer` is a named transport preference (see TRANSPORT_PREFERENCES) -- e.g.
    "transit" (the default: walk + public transit, no car to the station), "train"
    (suppress flights on rail-doable corridors), "drive", or "fastest". It selects
    the first/last-mile access and any suppressed modes; the response then offers up
    to `top_n` itineraries labelled by purpose (Fastest / Cheapest / Greenest /
    Fewest changes), each carrying its `labels`, so the UI can show and re-sort them.

    Each option is its itinerary's JSON plus `segments` (one coloured polyline per
    leg) and `labels`. With road=True and a resolvable region, car legs carry their
    real routed geometry. With timetable=None a timetable is auto-composed for the
    trip (flight network plus covering GTFS feeds); its coverage notes are surfaced
    as warnings.
    """
    pref = preference_kwargs(prefer)            # {"access", "exclude_modes"}
    access = pref["access"]
    o = _coerce(origin, geocoder=geocoder)
    d = _coerce(dest, geocoder=geocoder)
    # Capture the auto-compose notes (coverage gaps, feeds that could not be
    # fetched) so a missing-transit outcome reads as honest, not broken.
    # 'real streets' (road) only backs a car first/last mile. The 'car' and 'both'
    # presets have one; a walk-only 'transit' preset does not, so road is dropped
    # there (it would otherwise be rejected). turn_aware needs road, so it follows.
    primary_road = road and access != "transit"
    drove_to_hub = False

    def _plan(tt):
        """Rank choices for one timetable. Walk-only transit access can reach no hub
        (a far airport with no feeder service), dead-ending the preference; retry once
        pooling car access ('both') so the trip still routes. The 'both' arm has a car
        leg, so road can apply on that retry."""
        nonlocal drove_to_hub
        out = plan_trip_choices(
            o, d, depart_at, tt, objectives=_CHOICE_OBJECTIVES,
            road=primary_road, turn_aware=turn_aware and primary_road,
            region=region, data_dir=data_dir, geocoder=geocoder, **pref)
        if not out and access == "transit":
            out = plan_trip_choices(
                o, d, depart_at, tt, objectives=_CHOICE_OBJECTIVES,
                road=road, turn_aware=turn_aware and road, region=region,
                data_dir=data_dir, geocoder=geocoder, access="both",
                exclude_modes=pref["exclude_modes"])
            drove_to_hub = bool(out)
        return out

    with warnings_mod.catch_warnings(record=True) as caught:
        warnings_mod.simplefilter("always")
        labeled = _plan(timetable)
        # Coverage fallback: the prebuilt flight network is trimmed to well-connected
        # hubs (a route-count threshold), so a small regional airport (e.g. Santorini/
        # JTR, below it) can be absent and dead-end the trip. When an explicit timetable
        # found nothing, retry once with a trip-scoped flight network (airports near the
        # endpoints, no hub-degree filter), which includes those airports. Air-only
        # keeps it cheap -- no GTFS download.
        if not labeled and timetable is not None:
            from travelplanner.auto_timetable import build_default_timetable
            air_tt, _notes = build_default_timetable(o, d, ground=False, download=online)
            if air_tt.stops:
                labeled = _plan(air_tt)
    # Transit-first preferences (access == "transit": the transit and train presets)
    # lead with a rail/ferry option when one exists, so the headline card is the train
    # rather than a flight or drive that merely happens to be fastest. Stable, so the
    # remaining choices keep their by-objective order behind it; no effect when no
    # ground line-haul is present (e.g. a long-haul that can only fly).
    if access == "transit":
        labeled.sort(key=lambda il: 0 if any(
            leg.mode in (Mode.TRAIN, Mode.FERRY) for leg in il[0].legs) else 1)
    labeled = labeled[:top_n]
    warnings: list = [str(w.message) for w in caught]
    # Be honest when 'real streets' was asked for but had no car leg to back.
    if road and access == "transit" and not drove_to_hub:
        warnings.append("'real streets' applies to car legs only; the transit "
                        "preference has none, so it was not used.")
    itineraries = [it for it, _ in labeled]
    options = []
    for it, labels in labeled:
        opt = it.to_dict(with_legs=True)
        # Each leg already carries its routed polyline when a road-backed connector
        # produced one (road=True); the map follows it, else draws a straight line.
        opt["segments"] = itinerary_segments(it)
        opt["labels"] = list(labels)
        options.append(opt)
    # Explain the two confusing outcomes a user actually hits, so an empty or
    # car-only result reads as honest rather than broken.
    if not options:
        warnings.append(
            "No route found in the demo data near these points -- there may be "
            "no flights or transit within reach. Try larger cities/airports, or "
            "points closer to a hub.")
    elif access in ("transit", "both") and not any(
            leg.mode in LINE_HAUL_MODES for it in itineraries for leg in it.legs):
        # name the real fallback mode: a sub-threshold door-to-door trip is a
        # WALK, not a drive, and access='both' can return both -- so "driving"
        # alone would be inaccurate.
        legs = [leg for it in itineraries for leg in it.legs]
        has_car = any(leg.mode is Mode.CAR for leg in legs)
        has_walk = any(leg.mode is Mode.WALK for leg in legs)
        if has_car and not has_walk:
            mode_word = "driving"
        elif has_walk and not has_car:
            mode_word = "walking"
        else:
            mode_word = "travel"
        warnings.append(
            "No transit or flights reachable from these points; showing direct "
            + mode_word + " only.")
    elif (timetable is None
          and _RAIL_PLAUSIBLE_MIN_KM <= haversine(o.lat, o.lon, d.lat, d.lon)
            <= _RAIL_PLAUSIBLE_MAX_KM
          and not any(leg.mode is Mode.TRAIN
                      for it in itineraries for leg in it.legs)):
        # Auto-sourced transit (timetable=None) but no train across a corridor where an
        # intercity train would normally exist: say so, so an absent train (uneven GTFS
        # coverage) does not read as a verdict (e.g. Rome->Florence, where the
        # Frecciarossa exists but is not in the catalog feed for this route).
        warnings.append(
            "No train in these results -- there may be rail on this route we have no "
            "data for (GTFS coverage is uneven). Showing flights/driving only.")
    if drove_to_hub:
        warnings.append(
            "No walk-up transit to a hub from here, so these reach the "
            "airport/station by car.")
    return {
        "origin": o.to_dict(),
        "dest": d.to_dict(),
        "depart_at": depart_at.isoformat(),
        "prefer": prefer,
        "options": options,
        "warnings": sorted(set(warnings)),
    }


_UI_HTML = """<!doctype html><html><head><meta charset="utf-8">
<title>travelplanner demo</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
 html,body{height:100%;margin:0;font:14px/1.45 system-ui,sans-serif;color:#1a202c}
 #app{display:flex;height:100%}
 #side{width:360px;min-width:360px;overflow:auto;padding:16px;box-sizing:border-box;
   border-right:1px solid #e2e8f0;background:#f7fafc}
 #map{flex:1}
 h1{font-size:17px;margin:0 0 4px}
 .sub{color:#718096;font-size:12px;margin:0 0 12px}
 label{display:block;font-weight:600;margin:10px 0 3px;font-size:12px;color:#4a5568}
 input,select{width:100%;padding:8px;box-sizing:border-box;border:1px solid #cbd5e0;
   border-radius:6px;font:inherit;background:#fff}
 input:focus,select:focus{outline:0;border-color:#3182ce;box-shadow:0 0 0 2px rgba(49,130,206,.2)}
 .row{display:flex;gap:8px}.row>*{flex:1}
 .field{position:relative}
 .ac{position:absolute;left:0;right:0;top:100%;z-index:1200;background:#fff;
   border:1px solid #cbd5e0;border-top:0;border-radius:0 0 6px 6px;max-height:230px;
   overflow:auto;box-shadow:0 8px 18px rgba(0,0,0,.14);display:none}
 .ac.open{display:block}
 .ac .it{padding:7px 9px;cursor:pointer;font-size:13px;border-bottom:1px solid #f0f4f8}
 .ac .it:last-child{border-bottom:0}
 .ac .it.active{background:#ebf2fb}
 .ac .it .src{float:right;color:#a0aec0;font-size:9px;letter-spacing:.5px;
   text-transform:uppercase;margin-top:2px}
 .chk{display:flex;align-items:center;gap:6px;margin-top:12px;font-weight:600;font-size:12px}
 .chk input{width:auto}
 button{margin-top:12px;width:100%;padding:10px;border:0;border-radius:6px;
   background:#2b6cb0;color:#fff;font-weight:600;cursor:pointer}
 button:hover{background:#2c5282} button:disabled{background:#a0aec0;cursor:default}
 button.alt{background:#718096;margin-top:6px} button.alt:hover{background:#5a6678}
 #status{margin-top:10px;font-size:12px;color:#718096;white-space:pre-wrap}
 #status.err{color:#c53030}
 .opt{border:1px solid #e2e8f0;border-radius:8px;padding:9px;margin-top:8px;cursor:pointer;
   background:#fff}
 .opt:hover{border-color:#cbd5e0}
 .opt.sel{border-color:#2b6cb0;box-shadow:0 0 0 2px rgba(43,108,176,.2)}
 .opt .t{font-weight:700}.opt .m{color:#718096;font-size:12px;margin-top:2px}
 .chip{display:inline-block;padding:1px 7px;border-radius:10px;color:#fff;
   font-size:11px;margin:3px 3px 0 0}
 .lab-chip{display:inline-block;padding:1px 7px;border-radius:10px;background:#2b6cb0;
   color:#fff;font-size:11px;font-weight:700;margin-right:5px}
 .sortbar{margin:10px 0 2px;font-size:12px;color:#718096}
 .sortbar .lab{display:inline-block;width:auto;margin:2px 3px 0 0;padding:3px 9px;
   background:#edf2f7;color:#2b6cb0;border:1px solid #cbd5e0;border-radius:11px;
   font:inherit;font-size:11px;font-weight:600;cursor:pointer}
 .sortbar .lab:hover{background:#e2e8f0}
 .examples{display:flex;flex-wrap:wrap;gap:6px;margin-top:4px}
 .examples .ex{width:auto;padding:5px 10px;margin:0;background:#edf2f7;color:#2b6cb0;
   border:1px solid #cbd5e0;border-radius:13px;font:inherit;font-size:12px;
   font-weight:600;cursor:pointer}
 .examples .ex:hover{background:#e2e8f0}
 .legend{background:rgba(255,255,255,.92);padding:6px 9px;border-radius:6px;
   box-shadow:0 1px 5px rgba(0,0,0,.2);font-size:11px;line-height:1.7;color:#4a5568}
 .legend span{display:flex;align-items:center;gap:6px}
 .legend i{width:14px;height:4px;border-radius:2px;display:inline-block}
 .leg{font-size:12px;color:#4a5568;margin-top:3px}
 .sw{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px;
   vertical-align:middle}
 .foot{margin-top:16px;font-size:11px;color:#a0aec0;line-height:1.4}
 .ep{font-size:12px;color:#4a5568;font-weight:600;margin:6px 0 2px}
</style></head><body>
<div id="app">
 <div id="side">
  <h1>travelplanner</h1>
  <p class="sub">Type a place and pick a suggestion, or paste <code>lat,lon</code>.</p>
  <div class="field">
   <label>Origin</label>
   <input id="origin" autocomplete="off" placeholder="Start typing a place...">
   <div id="origin-ac" class="ac"></div>
  </div>
  <div class="field">
   <label>Destination</label>
   <input id="dest" autocomplete="off" placeholder="Start typing a place...">
   <div id="dest-ac" class="ac"></div>
  </div>
  <label>Depart</label>
  <input id="depart" type="datetime-local" value="DEFAULT_DEPART">
  <label>Preferred transport</label>
  <select id="prefer" title="how you like to travel; the choices below are planned for it">
   <option value="transit" title="walk to stops, no car to the station; trains/buses/ferries, and a flight only when it is far">Public transit</option>
   <option value="train" title="rail and ferry; flights are hidden unless there is no same-day train">Trains, avoid flying</option>
   <option value="drive" title="drive the whole way, or drive to the airport/station">Driving</option>
   <option value="fastest" title="no preference: the quickest door to door, including flights">Fastest, any mode</option>
  </select>
  <label>Choices to show</label>
  <input id="top" type="number" min="1" max="9" value="4">
  <label class="chk"><input type="checkbox" id="road" ROAD_CHECKED> real streets (car legs, auto-downloads map data)</label>
  <label class="chk"><input type="checkbox" id="transit" TRANSIT_CHECKED> trains &amp; buses (auto-downloads schedule data; first run slower)</label>
  <button id="go">Plan trip</button>
  <label>Examples</label>
  <div id="examples" class="examples"></div>
  <div id="status"></div>
  <div id="results"></div>
  <div class="foot">Suggestions: bundled cities, airports, feed stations &amp;
   OpenStreetMap. Itineraries are estimates over the loaded timetable.</div>
 </div>
 <div id="map"></div>
</div>
<script>
const colors = MODE_COLORS_JSON;
const map = L.map('map').setView([47,8], 5);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
  {maxZoom:19, attribution:'&copy; OpenStreetMap'}).addTo(map);

// Mode-colour legend so the route lines are readable at a glance.
const legend = L.control({position:'bottomright'});
legend.onAdd = () => {
  const div = L.DomUtil.create('div', 'legend');
  div.innerHTML = ['walk','car','train','ferry','flight']
    .filter(m => colors[m])
    .map(m => '<span><i style="background:'+colors[m]+'"></i>'+m+'</span>').join('');
  return div;
};
legend.addTo(map);
let drawn = [];
const selected = {};                 // inputId -> {label, lat, lon}

const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"]/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function setStatus(msg, isErr){ const s=$('status'); s.textContent=msg||'';
  s.className = isErr ? 'err' : ''; }
function debounce(fn, ms){ let t; return (...a)=>{clearTimeout(t);
  t=setTimeout(()=>fn(...a), ms);}; }
function fmtDur(mins){ mins=Math.round(mins); const h=Math.floor(mins/60), m=mins%60;
  return h ? (m ? h+'h '+m+'m' : h+'h') : m+'m'; }
const hhmm = s => s ? new Date(s).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}) : '';
function fmtKm(km){ return km < 1 ? 'short walk' : km.toFixed(0)+' km'; }

const SRC_LABEL = {city:'CITY', airport:'AIRPORT', station:'STATION',
  osm:'PLACE', recent:'RECENT'};
function getRecents(){
  try { return JSON.parse(localStorage.getItem('tp_recents') || '[]'); }
  catch(e){ return []; }                       // localStorage may be unavailable
}
function addRecent(s){
  const r = getRecents().filter(x => x.label !== s.label);
  r.unshift({label:s.label, lat:s.lat, lon:s.lon, source:'recent'});
  try { localStorage.setItem('tp_recents', JSON.stringify(r.slice(0,6))); }
  catch(e){ /* private mode / quota: skip persisting */ }
}


// --- autocomplete ---------------------------------------------------------
function attachAC(id){
  const input = $(id), box = $(id+'-ac');
  let items = [], active = -1;
  const close = () => { box.classList.remove('open'); box.innerHTML=''; items=[]; active=-1; };
  const hi = () => box.querySelectorAll('.it').forEach((el,i) =>
    el.classList.toggle('active', i===active));
  function choose(i){ const s=items[i]; if(!s) return;
    input.value=s.label; selected[id]={label:s.label,lat:s.lat,lon:s.lon};
    addRecent(s); close(); }
  function render(list){
    items=list; active=-1;
    if(!list.length){ close(); return; }
    box.innerHTML = list.map((s,i) =>
      '<div class="it" data-i="'+i+'">'+esc(s.label)
      +'<span class="src">'+(SRC_LABEL[s.source]||esc(s.source))+'</span></div>').join('');
    box.classList.add('open');
    box.querySelectorAll('.it').forEach(el =>
      el.addEventListener('mousedown', e => { e.preventDefault(); choose(+el.dataset.i); }));
  }
  function showRecents(){ const r = getRecents(); if(r.length) render(r); else close(); }
  const fetchSugg = debounce(async () => {
    const q = input.value.trim();
    if(q.length < 2 || /^[-+]?[0-9]/.test(q)){ close(); return; }   // skip coords/short
    try {
      const r = await fetch('/api/geocode?q='+encodeURIComponent(q));
      const d = await r.json();
      if(input.value.trim() === q) render(d.suggestions || []);
    } catch(e){ close(); }
  }, 350);
  input.addEventListener('input', () => {
    if(selected[id] && input.value !== selected[id].label) delete selected[id];
    if(!input.value.trim()){ showRecents(); return; }
    fetchSugg();
  });
  input.addEventListener('focus', () => { if(!input.value.trim()) showRecents(); });
  input.addEventListener('keydown', e => {
    const open = box.classList.contains('open');
    if(open && e.key==='ArrowDown'){ e.preventDefault(); active=Math.min(active+1,items.length-1); hi(); }
    else if(open && e.key==='ArrowUp'){ e.preventDefault(); active=Math.max(active-1,0); hi(); }
    else if(e.key==='Enter'){ e.preventDefault();
      if(open && active>=0) choose(active); else plan(); }
    else if(e.key==='Escape'){ close(); }
  });
  input.addEventListener('blur', () => setTimeout(close, 150));
}
function coordFor(id){
  const v = $(id).value.trim(), s = selected[id];
  return (s && v === s.label) ? (s.lat+','+s.lon) : v;   // exact coords if picked
}
attachAC('origin'); attachAC('dest');

// --- map + results --------------------------------------------------------
function clearMap(){ drawn.forEach(l => map.removeLayer(l)); drawn = []; }

// Points along the great circle from a to b ([lat,lon] each), so a flight is drawn
// as the arc it actually flies rather than a straight Mercator line. Longitudes are
// unwrapped to stay continuous, so a route crossing the date line does not streak.
function greatCircle(a, b, n){
  const R = Math.PI/180, D = 180/Math.PI;
  const la1=a[0]*R, lo1=a[1]*R, la2=b[0]*R, lo2=b[1]*R;
  const sdLa=Math.sin((la2-la1)/2), sdLo=Math.sin((lo2-lo1)/2);
  const h = sdLa*sdLa + Math.cos(la1)*Math.cos(la2)*sdLo*sdLo;
  const d = 2*Math.asin(Math.min(1, Math.sqrt(h)));      // angular distance
  if(d === 0) return [a, b];
  const pts = []; let prevLon = null;
  for(let i=0;i<=n;i++){
    const f=i/n, A=Math.sin((1-f)*d)/Math.sin(d), B=Math.sin(f*d)/Math.sin(d);
    const x=A*Math.cos(la1)*Math.cos(lo1)+B*Math.cos(la2)*Math.cos(lo2);
    const y=A*Math.cos(la1)*Math.sin(lo1)+B*Math.cos(la2)*Math.sin(lo2);
    const z=A*Math.sin(la1)+B*Math.sin(la2);
    let lon=Math.atan2(y,x)*D;
    if(prevLon !== null){ while(lon-prevLon>180) lon-=360; while(lon-prevLon<-180) lon+=360; }
    prevLon = lon;
    pts.push([Math.atan2(z, Math.sqrt(x*x+y*y))*D, lon]);
  }
  return pts;
}

function drawOption(data, idx){
  clearMap();
  const opt = data.options[idx], grp = [];
  opt.segments.forEach(s => {
    // ground legs drawn as a single straight line are estimates, not road routes
    const estimate = s.coords.length <= 2 && (s.mode === 'car' || s.mode === 'walk');
    const style = {color:s.color, weight:6, opacity: estimate ? 0.65 : 0.85};
    if(estimate) style.dashArray = '8,8';
    // a flight follows a great-circle arc, not the straight line between its airports
    const coords = (s.mode === 'flight' && s.coords.length === 2)
      ? greatCircle(s.coords[0], s.coords[1], 48) : s.coords;
    const line = L.polyline(coords, style).addTo(map);
    line.bindPopup(s.label + (estimate ? ' (straight-line estimate)' : ''));
    drawn.push(line); grp.push(line);
  });
  const o = data.origin, d = data.dest;
  drawn.push(L.marker([o.lat,o.lon]).addTo(map).bindPopup('Origin: '+esc(o.name)));
  drawn.push(L.marker([d.lat,d.lon]).addTo(map).bindPopup('Destination: '+esc(d.name)));
  if(grp.length) map.fitBounds(L.featureGroup(grp).getBounds(), {padding:[40,40]});
  document.querySelectorAll('.opt').forEach((e,i) => e.classList.toggle('sel', i===idx));
}

function endpointsLine(data){
  const o = data.origin, d = data.dest;
  return 'From ' + esc(o.name) + ' (' + o.lat.toFixed(2) + ', ' + o.lon.toFixed(2)
    + ') to ' + esc(d.name) + ' (' + d.lat.toFixed(2) + ', ' + d.lon.toFixed(2) + ')';
}

function renderResults(data){
  const box = $('results'); box.innerHTML = '';
  const warn = (data.warnings && data.warnings.length)
    ? '\\n' + data.warnings.join('\\n') : '';
  if(!data.options.length){
    box.innerHTML = '<div class="ep">'+endpointsLine(data)+'</div>'
      + '<p>No route found for these inputs.</p>';
    clearMap(); setStatus((data.warnings && data.warnings[0]) || 'No route found.',
      true);
    return; }
  box.innerHTML = '<div class="ep">'+endpointsLine(data)+'</div>';
  const present = sortLabels(data);
  if(present.length > 1){
    const bar = document.createElement('div'); bar.className = 'sortbar';
    bar.append('Sort: ');
    present.forEach(l => {
      const b = document.createElement('button'); b.className = 'lab';
      b.textContent = l; b.onclick = () => sortByLabel(data, l);
      bar.appendChild(b);
    });
    box.appendChild(bar);
  }
  data.options.forEach((opt, i) => {
    const div = document.createElement('div'); div.className = 'opt';
    const arr = new Date(opt.arrive_at).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});
    const modes = [...new Set(opt.legs.map(l => l.mode))].map(m =>
      '<span class="chip" style="background:'+(colors[m]||'#000')+'">'+m+'</span>').join('');
    const legs = opt.legs.map(l => {
      const t = (l.depart_at && l.arrive_at)
        ? hhmm(l.depart_at)+'&ndash;'+hhmm(l.arrive_at)+' ' : '';
      const dur = l.travel_time_s != null ? ' &middot; '+fmtDur(l.travel_time_s/60) : '';
      return '<div class="leg"><span class="sw" style="background:'+(colors[l.mode]||'#000')+'"></span>'
        + t+esc(l.mode)+': '+esc(l.from.name)+' &rarr; '+esc(l.to.name)
        + dur+' ('+fmtKm(l.distance_km)+')</div>';
    }).join('');
    const fare = opt.fare_estimate != null
      ? ' &middot; ~'+Math.round(opt.fare_estimate)+' '+esc(opt.fare_currency) : '';
    const labelChips = (opt.labels||[]).map(l =>
      '<span class="lab-chip">'+esc(l)+'</span>').join('');
    div.innerHTML = '<div class="t">'+(labelChips || ('Option '+(i+1)+' '))
      + fmtDur(opt.total_minutes)+'</div>'
      + '<div class="m">'+opt.num_transfers+' transfer(s) &middot; cost '+esc(opt.cost_level)
      + fare + ' &middot; arrive '+arr+'</div><div>'+modes+'</div>'+legs;
    div.onclick = () => drawOption(data, i);
    box.appendChild(div);
  });
  setStatus(data.options.length+' choice(s). Click a card to highlight it'
    + (sortLabels(data).length>1 ? '; use Sort to reorder.' : '.') + warn);
  drawOption(data, 0);
}

function sortLabels(data){
  return [...new Set(data.options.flatMap(o => o.labels||[]))];
}

function sortByLabel(data, label){
  const i = data.options.findIndex(o => (o.labels||[]).includes(label));
  if(i > 0){ const [picked] = data.options.splice(i, 1); data.options.unshift(picked); }
  renderResults(data);
}

async function plan(){
  const origin = coordFor('origin'), dest = coordFor('dest');
  if(!origin || !dest){ setStatus('Enter an origin and a destination.', true); return; }
  const p = new URLSearchParams({origin, dest, prefer:$('prefer').value,
    top:$('top').value, depart:$('depart').value});
  if($('road').checked) p.set('road','1');   // region auto-resolved from the coordinates
  if($('transit').checked) p.set('transit','1');
  const btn = $('go'), label = btn.textContent;
  btn.disabled = true; btn.textContent = 'Planning...';
  setStatus($('road').checked || $('transit').checked
    ? 'Planning... downloading map/schedule data for new areas, so the first request for a region can take a minute.'
    : 'Planning...');
  try {
    const r = await fetch('/api/plan?'+p.toString());
    const data = await r.json();
    if(!r.ok){ setStatus('Error: '+(data.error||r.status), true); return; }
    renderResults(data);
  } catch(e){ setStatus('Request failed: '+e, true); }
  finally { btn.disabled = false; btn.textContent = label; }
}

function applyExample(ex){
  delete selected.origin; delete selected.dest;
  $('origin').value = ex.origin; $('dest').value = ex.dest;
  if(ex.depart) $('depart').value = ex.depart;
  if(ex.prefer) $('prefer').value = ex.prefer;        // does not persist; an example is a one-off
  if(ex.transit) $('transit').checked = true;         // a rail example turns schedules on; it never unticks your choice
  plan();
}

async function loadExamples(){
  try {
    const r = await fetch('/api/examples'); const data = await r.json();
    const box = $('examples'); box.innerHTML = '';
    (data.examples || []).forEach(ex => {
      const b = document.createElement('button'); b.className = 'ex';
      b.textContent = ex.label; b.title = 'Load this example trip';
      b.onclick = () => applyExample(ex);
      box.appendChild(b);
    });
  } catch(e){ console.warn('examples unavailable:', e); }  // optional row; degrade quietly
}

$('go').onclick = plan;
loadExamples();

// Remember the transport preference across visits (set once, it sticks). Guarded:
// localStorage can throw in private modes, which must not break the page.
const PREF_KEY = 'tp_prefer';
try {
  const saved = localStorage.getItem(PREF_KEY);
  if(saved && [...$('prefer').options].some(o => o.value === saved)) $('prefer').value = saved;
  $('prefer').onchange = () => { try { localStorage.setItem(PREF_KEY, $('prefer').value); } catch(e){} };
} catch(e){}
</script></body></html>"""


def _ui_html(default_depart: datetime | None = None, online: bool = True) -> str:
    depart = default_depart.strftime("%Y-%m-%dT%H:%M") if default_depart else ""
    # Default the trains & buses and real streets toggles on so the demo shows transit
    # and real road geometry out of the box -- but only when online, since both
    # auto-download data (schedules per trip, an OSM region for road) on first use.
    checked = "checked" if online else ""
    return (_UI_HTML
            .replace("MODE_COLORS_JSON", json.dumps(MODE_COLORS))
            .replace("DEFAULT_DEPART", depart)
            .replace("TRANSIT_CHECKED", checked)
            .replace("ROAD_CHECKED", checked))


class _Handler(BaseHTTPRequestHandler):
    server_version = "travelplanner-demo"

    def _send(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, status: int = 200) -> None:
        self._send(json.dumps(obj).encode("utf-8"), "application/json", status)

    def do_GET(self) -> None:    # noqa: N802 (http.server API)
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            self._send(_ui_html(self.server.default_depart,
                                self.server.online).encode("utf-8"),
                       "text/html; charset=utf-8")
        elif path == "/api/health":
            self._json({"status": "ok"})
        elif path == "/api/example":
            example = dict(self.server.example)
            example["depart"] = self.server.default_depart.strftime("%Y-%m-%dT%H:%M")
            self._json(example)
        elif path == "/api/examples":
            depart = self.server.default_depart.strftime("%Y-%m-%dT%H:%M")
            examples = [dict(ex, depart=depart) for ex in self.server.examples]
            self._json({"examples": examples})
        elif path == "/api/geocode":
            self._handle_geocode(parse_qs(parsed.query))
        elif path == "/api/plan":
            self._handle_plan(parse_qs(parsed.query))
        else:
            self._json({"error": "not found"}, 404)

    def _handle_geocode(self, query: dict) -> None:
        q = (query.get("q") or [""])[0]
        try:
            limit = max(1, min(10, int((query.get("limit") or ["8"])[0])))
        except ValueError:
            limit = 8
        self._json({"suggestions": _geocode_suggestions(self.server, q, limit)})

    def _handle_plan(self, query: dict) -> None:
        def first(name, default=None):
            values = query.get(name)
            return values[0] if values else default

        origin, dest = first("origin"), first("dest")
        if not origin or not dest:
            self._json({"error": "origin and dest are required"}, 400)
            return
        srv = self.server
        try:
            depart_at = _parse_depart(first("depart"), srv.default_depart)
            top_n = max(1, min(9, int(first("top", "3"))))
            road = first("road", "") in ("1", "true", "yes", "on")
            transit = first("transit", "") in ("1", "true", "yes", "on")
            prefer = first("prefer", DEFAULT_TRANSPORT_PREFERENCE)
            region = first("region") or srv.region
            # Serve an identical earlier request from the LRU cache (instant), keyed by
            # everything that shapes the plan. The other inputs (data_dir, turn_aware,
            # geocoder, online) are fixed for the server's lifetime.
            cache_key = (origin, dest, depart_at, prefer, top_n, road, transit, region)
            cached = srv.plan_cache.get(cache_key)
            if cached is not None:
                srv.plan_cache.move_to_end(cache_key)
                self._json(cached)
                return
            # transit on -> auto-compose a trip-scoped timetable (flight network +
            # covering GTFS feeds) so trains/buses appear; off -> the prebuilt
            # flight-only feed (fast, no per-request feed download).
            timetable = None if transit else srv.timetable
            response = plan_response(
                origin, dest, depart_at, timetable, prefer=prefer,
                top_n=top_n, road=road, region=region, data_dir=srv.data_dir,
                turn_aware=srv.turn_aware, geocoder=srv.geocoder, online=srv.online)
        except (ValueError, KeyError) as exc:
            self._json({"error": str(exc)}, 400)
            return
        srv.plan_cache[cache_key] = response
        if len(srv.plan_cache) > _PLAN_CACHE_MAX:
            srv.plan_cache.popitem(last=False)        # evict least-recently-used
        self._json(response)

    def log_message(self, fmt, *args) -> None:    # quieter: one concise line
        if self.server.verbose:
            super().log_message(fmt, *args)


def make_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, *,
                timetable=None, region: str | None = None,
                data_dir: str | None = None, turn_aware: bool = False,
                geocoder=None, online: bool = True, user_agent: str = USER_AGENT,
                default_depart: datetime | None = None,
                verbose: bool = False) -> HTTPServer:
    """Build (but do not start) the demo server; defaults to the sample feed.

    Single-threaded on purpose: the routingkit CCH road routers are thread-affine
    (a cached query may only run on the thread that created it), so all requests
    are served on one thread. Requests therefore serialise -- fine for a demo; a
    long road-graph build blocks other requests until it finishes.

    `online` (default True) enables the Nominatim-backed geocoder + autocomplete
    so arbitrary place names resolve; `online=False` stays bundled-only/offline.
    Pass an explicit `geocoder` to override the planning geocoder entirely.
    """
    if timetable is None:
        timetable, source = _default_timetable(online)
    else:
        source = ("flights" if len(timetable.stops) >= _FLIGHT_NETWORK_MIN_STOPS
                  else "sample")
    if default_depart is None:
        # next 08:00 (synthetic flights depart through the day); a sensible,
        # always-in-range default for the daily flight network.
        default_depart = datetime.now().replace(hour=8, minute=0, second=0,
                                                microsecond=0)
        if default_depart < datetime.now():
            default_depart += timedelta(days=1)
    server = HTTPServer((host, port), _Handler)
    server.timetable = timetable
    server.region = region
    server.data_dir = data_dir
    server.turn_aware = turn_aware
    server.geocoder = geocoder or _build_geocoder(online, user_agent)
    server.online = online
    server.user_agent = user_agent
    server.example = _CITY_EXAMPLE if source == "flights" else _SAMPLE_EXAMPLE
    server.examples = _FLIGHT_EXAMPLES if source == "flights" else _SAMPLE_EXAMPLES
    try:
        server.airports = load_airports(download=online)   # cached OpenFlights airports
    except (FileNotFoundError, ValueError, OSError):
        # offline with no cache: autocomplete simply omits airport suggestions
        # rather than crashing startup (mirrors the timetable fallback above).
        server.airports = ()
    server.geo_cache = {}
    # Identical plan requests (a repeated example click, a re-submit) return the cached
    # response instead of re-planning -- a plan is a couple of seconds, so this makes a
    # repeat instant. Bounded LRU; per-process, so it clears on restart (no staleness
    # across a feed refresh).
    server.plan_cache = OrderedDict()
    server.last_nominatim = 0.0
    server.default_depart = default_depart
    server.verbose = verbose
    return server


def serve(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, **kwargs) -> None:
    """Start the demo server and block until interrupted (Ctrl-C)."""
    server = make_server(host, port, verbose=True, **kwargs)
    print(f"travelplanner demo on http://{host}:{port}  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping...")
    finally:
        server.server_close()


def _enable_continent_roads(data_dir: str | None) -> None:
    """Wire a continent road graph so cross-border drives follow real highways: an
    explicit --continent-roads dir, else the auto-built europe-highway artifact if
    present. Left off when neither exists (cross-border drive stays a straight line).
    """
    from travelplanner.roads import cache_dir, set_continent_road

    if data_dir is None:
        default = os.path.join(cache_dir(), "artifacts", "europe-highway")
        if os.path.exists(os.path.join(default, "meta.json")):
            data_dir = default
    if data_dir is not None:
        set_continent_road("europe", data_dir)
        print(f"cross-border drives: routing over {data_dir}")


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(
        description="travelplanner demo API + map UI (pure stdlib)")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--region", default=None,
                        help="Geofabrik region for road-backed car legs")
    parser.add_argument("--data-dir", default=None,
                        help="offline road artifact dir (build_region output)")
    parser.add_argument("--continent-roads", default=None,
                        help="road artifact dir for cross-border drives "
                             "(default: auto-detect the europe-highway artifact)")
    parser.add_argument("--turn-aware", action="store_true",
                        help="route car legs over the turn-aware graph")
    parser.add_argument("--offline", action="store_true",
                        help="bundled cities only; no Nominatim/network geocoding")
    args = parser.parse_args(argv)
    _enable_continent_roads(args.continent_roads)
    serve(args.host, args.port, region=args.region, data_dir=args.data_dir,
          turn_aware=args.turn_aware, online=not args.offline)


if __name__ == "__main__":
    main()
