"""Demo HTTP service: a small JSON API + a Leaflet map UI for trip planning.

Pure stdlib (http.server) -- no extra dependencies. Start it with:

    python -m travelplanner.service               # http://127.0.0.1:8000
    python -m travelplanner.service --region switzerland   # road-backed car legs

Endpoints:
    GET /                  the map UI (enter origin/dest, see ranked trips)
    GET /api/plan?origin=&dest=&depart=&objective=&access=&top=&road=&region=
                           ranked itineraries as JSON, each with per-leg map
                           segments ({coords, mode, color, label})
    GET /api/example       a ready-made origin/dest/depart for the bundled feed
    GET /api/health        {"status": "ok"}

The default plans over the bundled sample timetable with straight-line legs, so
the demo runs offline with no downloads. Pass a `region` (per request or via
--region) to follow real streets on car legs (downloads/builds that extract).
"""

import argparse
import json
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from travelplanner.catalog import search_cities
from travelplanner.geocoding import (bundled_geocoder, cached, chain,
                                     nominatim_geocoder, nominatim_search)
from travelplanner.models import LINE_HAUL_MODES, Mode
from travelplanner.graph.query import Objective
from travelplanner.graph.schema import NodeType
from travelplanner.openflights import (load_airports, load_flight_network,
                                       search_airports)
from travelplanner.roads import _coerce, drive_route
from travelplanner.samples import sample_timetable
from travelplanner.trips import plan_trip
from travelplanner.viz import MODE_COLORS, itinerary_segments

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
# Identify the demo to Nominatim (required by their usage policy).
USER_AGENT = "travelplanner-demo/1.0 (+https://github.com/bgokden/travelplanner)"
# Nominatim asks for at most ~1 request/second; throttle network suggestions.
_NOMINATIM_MIN_INTERVAL = 1.0


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
    seen: set = set()              # rounded coords already offered
    seen_labels: set = set()       # exact labels already offered (drop OSM dups)
    for row in search_cities(q, limit=limit):
        label = ", ".join(p for p in (row["name"], row["country"]) if p)
        out.append({"label": label, "lat": row["lat"], "lon": row["lon"],
                    "source": "city"})
        seen.add((round(row["lat"], 3), round(row["lon"], 3)))
        seen_labels.add(label.lower())
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
        seen_labels.add(label.lower())
    for stop in _search_stops(server.timetable, q, limit):
        if len(out) >= limit:
            break
        ck = (round(stop.lat, 3), round(stop.lon, 3))
        if ck in seen:
            continue
        seen.add(ck)
        out.append({"label": stop.name, "lat": stop.lat, "lon": stop.lon,
                    "source": "station"})
        seen_labels.add(stop.name.lower())
    cacheable = True
    if server.online and len(out) < limit:
        now = time.monotonic()
        if now - server.last_nominatim >= _NOMINATIM_MIN_INTERVAL:
            server.last_nominatim = now
            for r in nominatim_search(q, user_agent=server.user_agent, limit=limit):
                ck = (round(r["lat"], 3), round(r["lon"], 3))
                if ck in seen or r["name"].lower() in seen_labels:
                    continue
                seen.add(ck)
                seen_labels.add(r["name"].lower())
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


def _road_geometries(itinerary, *, region, data_dir, depart_at, turn_aware):
    """Real routed (lat, lon) paths per CAR leg, keyed by 1-based leg index.

    Returns (geometries, warnings). A leg whose region cannot be resolved (no
    coverage / cross-border) is skipped with a warning rather than failing the
    whole request, so the map still draws the trip with a straight car leg.
    """
    geometries: dict = {}
    warnings: list = []
    for i, leg in enumerate(itinerary.legs, 1):
        if leg.mode is not Mode.CAR:
            continue
        try:
            route = drive_route(leg.from_loc, leg.to_loc, region=region,
                                data_dir=data_dir, depart_at=depart_at,
                                turn_aware=turn_aware)
        except ValueError as exc:
            warnings.append(f"road geometry unavailable for a car leg: {exc}")
            continue
        if route.drivable and len(route.geometry) >= 2:
            geometries[i] = [[lat, lon] for lat, lon in route.geometry]
    return geometries, warnings


def plan_response(origin, dest, depart_at: datetime, timetable, *,
                  objective: str = "air_priority", top_n: int = 3,
                  access: str = "car", road: bool = False,
                  region: str | None = None, data_dir: str | None = None,
                  turn_aware: bool = False, geocoder=None) -> dict:
    """Plan a door-to-door trip and shape it for the map UI (JSON-safe dict).

    Mirrors plan_trip's arguments; each ranked itinerary is returned with its
    JSON fields plus `segments` -- one coloured polyline per leg. With road=True
    and a resolvable region, car legs carry their real routed geometry.
    """
    obj = Objective(objective)
    o = _coerce(origin, geocoder=geocoder)
    d = _coerce(dest, geocoder=geocoder)
    itineraries = plan_trip(o, d, depart_at, timetable, objective=obj, top_n=top_n,
                            access=access, road=road, turn_aware=turn_aware,
                            region=region, data_dir=data_dir, geocoder=geocoder)
    warnings: list = []
    options = []
    for it in itineraries:
        geometries: dict = {}
        if road:
            geometries, warns = _road_geometries(
                it, region=region, data_dir=data_dir, depart_at=depart_at,
                turn_aware=turn_aware)
            warnings.extend(warns)
        opt = it.to_dict(with_legs=True)
        opt["segments"] = itinerary_segments(it, geometries)
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
        # WALK, not a drive, so "driving" would be inaccurate.
        drove = any(leg.mode is Mode.CAR for it in itineraries for leg in it.legs)
        warnings.append(
            "No transit or flights reachable from these points; showing direct "
            + ("driving" if drove else "walking") + " only.")
    return {
        "origin": o.to_dict(),
        "dest": d.to_dict(),
        "depart_at": depart_at.isoformat(),
        "objective": obj.value,
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
  <div class="row">
   <div><label>Objective</label>
    <select id="objective" title="how to rank options">
     <option value="air_priority" title="prefer flights">air priority</option>
     <option value="fastest" title="shortest total time">fastest</option>
     <option value="cheapest" title="lowest cost tier">cheapest</option>
     <option value="fewest_transfers" title="fewest vehicle changes">fewest transfers</option>
     <option value="greenest" title="least driving, then lowest emissions">greenest</option>
    </select></div>
   <div><label>Access</label>
    <select id="access">
     <option value="car">car</option>
     <option value="transit">transit</option>
     <option value="both">both</option>
    </select></div>
  </div>
  <label>Options</label>
  <input id="top" type="number" min="1" max="9" value="3">
  <label class="chk"><input type="checkbox" id="road"> real streets (car legs)</label>
  <input id="region" placeholder="region for roads, e.g. switzerland" style="display:none">
  <button id="go">Plan trip</button>
  <button id="ex" class="alt">Load example</button>
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

$('road').addEventListener('change', () => {
  $('region').style.display = $('road').checked ? 'block' : 'none';
});

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

function drawOption(data, idx){
  clearMap();
  const opt = data.options[idx], grp = [];
  opt.segments.forEach(s => {
    // ground legs drawn as a single straight line are estimates, not road routes
    const estimate = s.coords.length <= 2 && (s.mode === 'car' || s.mode === 'walk');
    const style = {color:s.color, weight:6, opacity: estimate ? 0.65 : 0.85};
    if(estimate) style.dashArray = '8,8';
    const line = L.polyline(s.coords, style).addTo(map);
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
  data.options.forEach((opt, i) => {
    const div = document.createElement('div'); div.className = 'opt';
    const arr = new Date(opt.arrive_at).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});
    const modes = [...new Set(opt.legs.map(l => l.mode))].map(m =>
      '<span class="chip" style="background:'+(colors[m]||'#000')+'">'+m+'</span>').join('');
    const legs = opt.legs.map(l =>
      '<div class="leg"><span class="sw" style="background:'+(colors[l.mode]||'#000')+'"></span>'
      + esc(l.mode)+': '+esc(l.from.name)+' &rarr; '+esc(l.to.name)
      + ' ('+fmtKm(l.distance_km)+')</div>').join('');
    div.innerHTML = '<div class="t">Option '+(i+1)+' &middot; '
      + fmtDur(opt.total_minutes)+'</div>'
      + '<div class="m">'+opt.num_transfers+' transfer(s) &middot; cost '+esc(opt.cost_level)
      + ' &middot; arrive '+arr+'</div><div>'+modes+'</div>'+legs;
    div.onclick = () => drawOption(data, i);
    box.appendChild(div);
  });
  setStatus(data.options.length+' option(s). Click one to highlight it.' + warn);
  drawOption(data, 0);
}

async function plan(){
  const origin = coordFor('origin'), dest = coordFor('dest');
  if(!origin || !dest){ setStatus('Enter an origin and a destination.', true); return; }
  const p = new URLSearchParams({origin, dest, objective:$('objective').value,
    access:$('access').value, top:$('top').value, depart:$('depart').value});
  if($('road').checked){ p.set('road','1');
    if($('region').value.trim()) p.set('region', $('region').value.trim()); }
  const btn = $('go'), label = btn.textContent;
  btn.disabled = true; btn.textContent = 'Planning...'; setStatus('Planning...');
  try {
    const r = await fetch('/api/plan?'+p.toString());
    const data = await r.json();
    if(!r.ok){ setStatus('Error: '+(data.error||r.status), true); return; }
    renderResults(data);
  } catch(e){ setStatus('Request failed: '+e, true); }
  finally { btn.disabled = false; btn.textContent = label; }
}

async function loadExample(){
  try {
    const r = await fetch('/api/example'); const ex = await r.json();
    delete selected.origin; delete selected.dest;
    $('origin').value = ex.origin; $('dest').value = ex.dest; $('depart').value = ex.depart;
    plan();
  } catch(e){ setStatus('Could not load example: '+e, true); }
}

$('go').onclick = plan;
$('ex').onclick = loadExample;
</script></body></html>"""


def _ui_html(default_depart: datetime | None = None) -> str:
    depart = default_depart.strftime("%Y-%m-%dT%H:%M") if default_depart else ""
    return (_UI_HTML
            .replace("MODE_COLORS_JSON", json.dumps(MODE_COLORS))
            .replace("DEFAULT_DEPART", depart))


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
            self._send(_ui_html(self.server.default_depart).encode("utf-8"),
                       "text/html; charset=utf-8")
        elif path == "/api/health":
            self._json({"status": "ok"})
        elif path == "/api/example":
            example = dict(self.server.example)
            example["depart"] = self.server.default_depart.strftime("%Y-%m-%dT%H:%M")
            self._json(example)
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
            response = plan_response(
                origin, dest, depart_at, srv.timetable,
                objective=first("objective", "air_priority"),
                access=first("access", "car"), top_n=top_n, road=road,
                region=(first("region") or srv.region), data_dir=srv.data_dir,
                turn_aware=srv.turn_aware, geocoder=srv.geocoder)
        except (ValueError, KeyError) as exc:
            self._json({"error": str(exc)}, 400)
            return
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
    try:
        server.airports = load_airports(download=online)   # cached OpenFlights airports
    except (FileNotFoundError, ValueError, OSError):
        # offline with no cache: autocomplete simply omits airport suggestions
        # rather than crashing startup (mirrors the timetable fallback above).
        server.airports = ()
    server.geo_cache = {}
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


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(
        description="travelplanner demo API + map UI (pure stdlib)")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--region", default=None,
                        help="Geofabrik region for road-backed car legs")
    parser.add_argument("--data-dir", default=None,
                        help="offline road artifact dir (build_region output)")
    parser.add_argument("--turn-aware", action="store_true",
                        help="route car legs over the turn-aware graph")
    parser.add_argument("--offline", action="store_true",
                        help="bundled cities only; no Nominatim/network geocoding")
    args = parser.parse_args(argv)
    serve(args.host, args.port, region=args.region, data_dir=args.data_dir,
          turn_aware=args.turn_aware, online=not args.offline)


if __name__ == "__main__":
    main()
