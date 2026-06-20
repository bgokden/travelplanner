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
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from travelplanner.models import Mode
from travelplanner.graph.query import Objective
from travelplanner.roads import _coerce, drive_route
from travelplanner.samples import sample_timetable, sample_trip
from travelplanner.trips import plan_trip
from travelplanner.viz import MODE_COLORS, itinerary_segments

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000

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
        if route.feasible and len(route.geometry) >= 2:
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
 html,body{height:100%;margin:0;font:14px/1.45 system-ui,sans-serif}
 #app{display:flex;height:100%}
 #side{width:340px;min-width:340px;overflow:auto;padding:14px;box-sizing:border-box;
   border-right:1px solid #ddd;background:#fafafa}
 #map{flex:1}
 h1{font-size:16px;margin:0 0 10px}
 label{display:block;font-weight:600;margin:8px 0 2px;font-size:12px;color:#444}
 input,select{width:100%;padding:6px;box-sizing:border-box;border:1px solid #ccc;
   border-radius:6px;font:inherit}
 .row{display:flex;gap:8px}.row>*{flex:1}
 .chk{display:flex;align-items:center;gap:6px;margin-top:10px;font-weight:600;font-size:12px}
 .chk input{width:auto}
 button{margin-top:12px;width:100%;padding:9px;border:0;border-radius:6px;
   background:#2b6cb0;color:#fff;font-weight:600;cursor:pointer}
 button.alt{background:#718096;margin-top:6px}
 #status{margin-top:10px;font-size:12px;color:#666;white-space:pre-wrap}
 .opt{border:1px solid #ddd;border-radius:8px;padding:8px;margin-top:8px;cursor:pointer;
   background:#fff}
 .opt.sel{border-color:#2b6cb0;box-shadow:0 0 0 2px rgba(43,108,176,.2)}
 .opt .t{font-weight:700}.opt .m{color:#555;font-size:12px;margin-top:2px}
 .chip{display:inline-block;padding:1px 7px;border-radius:10px;color:#fff;
   font-size:11px;margin:2px 3px 0 0}
 .leg{font-size:12px;color:#444;margin-top:3px}
 .sw{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px;
   vertical-align:middle}
</style></head><body>
<div id="app">
 <div id="side">
  <h1>travelplanner demo</h1>
  <label>Origin (name or lat,lon)</label>
  <input id="origin" placeholder="e.g. Zurich or 47.0,7.0">
  <label>Destination</label>
  <input id="dest" placeholder="e.g. Milan or 45.0,9.0">
  <label>Depart</label>
  <input id="depart" type="datetime-local">
  <div class="row">
   <div><label>Objective</label>
    <select id="objective">
     <option value="air_priority">air priority</option>
     <option value="fastest">fastest</option>
     <option value="cheapest">cheapest</option>
     <option value="fewest_transfers">fewest transfers</option>
     <option value="greenest">greenest</option>
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
 </div>
 <div id="map"></div>
</div>
<script>
const colors = MODE_COLORS_JSON;
const map = L.map('map').setView([47,8], 5);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
  {maxZoom:19, attribution:'&copy; OpenStreetMap'}).addTo(map);
let drawn = [];
let lastData = null;

const $ = id => document.getElementById(id);
$('road').addEventListener('change', () => {
  $('region').style.display = $('road').checked ? 'block' : 'none';
});

function clearMap(){ drawn.forEach(l => map.removeLayer(l)); drawn = []; }

function drawOption(data, idx){
  clearMap();
  const opt = data.options[idx];
  const grp = [];
  opt.segments.forEach(s => {
    const line = L.polyline(s.coords, {color:s.color, weight:6, opacity:.85}).addTo(map);
    line.bindPopup(s.label);
    drawn.push(line); grp.push(line);
  });
  const o = data.origin, d = data.dest;
  drawn.push(L.marker([o.lat,o.lon]).addTo(map).bindPopup('Origin: '+o.name));
  drawn.push(L.marker([d.lat,d.lon]).addTo(map).bindPopup('Destination: '+d.name));
  if(grp.length) map.fitBounds(L.featureGroup(grp).getBounds(), {padding:[40,40]});
  document.querySelectorAll('.opt').forEach((e,i) =>
    e.classList.toggle('sel', i===idx));
}

function renderResults(data){
  lastData = data;
  const box = $('results'); box.innerHTML = '';
  if(!data.options.length){ box.innerHTML = '<p>No route found.</p>'; clearMap(); return; }
  data.options.forEach((opt, i) => {
    const div = document.createElement('div');
    div.className = 'opt';
    const arr = new Date(opt.arrive_at).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});
    const modes = [...new Set(opt.legs.map(l => l.mode))].map(m =>
      '<span class="chip" style="background:'+(colors[m]||'#000')+'">'+m+'</span>').join('');
    const legs = opt.legs.map(l =>
      '<div class="leg"><span class="sw" style="background:'+(colors[l.mode]||'#000')+'"></span>'
      + l.mode+': '+l.from.name+' → '+l.to.name
      + ' ('+l.distance_km.toFixed(0)+' km)</div>').join('');
    div.innerHTML = '<div class="t">Option '+(i+1)+' · '
      + Math.round(opt.total_minutes)+' min</div>'
      + '<div class="m">'+opt.num_transfers+' transfer(s) · cost '+opt.cost_level
      + ' · arrive '+arr+'</div><div>'+modes+'</div>'+legs;
    div.onclick = () => drawOption(data, i);
    box.appendChild(div);
  });
  let st = data.options.length+' option(s).';
  if(data.warnings && data.warnings.length) st += '\\n'+data.warnings.join('\\n');
  $('status').textContent = st;
  drawOption(data, 0);
}

async function plan(){
  const origin = $('origin').value.trim(), dest = $('dest').value.trim();
  if(!origin || !dest){ $('status').textContent = 'Enter origin and destination.'; return; }
  const p = new URLSearchParams({origin, dest, objective:$('objective').value,
    access:$('access').value, top:$('top').value, depart:$('depart').value});
  if($('road').checked){ p.set('road','1'); if($('region').value.trim()) p.set('region',$('region').value.trim()); }
  $('status').textContent = 'Planning...';
  try {
    const r = await fetch('/api/plan?'+p.toString());
    const data = await r.json();
    if(!r.ok){ $('status').textContent = 'Error: '+(data.error||r.status); return; }
    renderResults(data);
  } catch(e){ $('status').textContent = 'Request failed: '+e; }
}

async function loadExample(){
  const r = await fetch('/api/example'); const ex = await r.json();
  $('origin').value = ex.origin; $('dest').value = ex.dest; $('depart').value = ex.depart;
  plan();
}

$('go').onclick = plan;
$('ex').onclick = loadExample;
</script></body></html>"""


def _ui_html() -> str:
    return _UI_HTML.replace("MODE_COLORS_JSON", json.dumps(MODE_COLORS))


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
            self._send(_ui_html().encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/health":
            self._json({"status": "ok"})
        elif path == "/api/example":
            o, d, dep = sample_trip()
            self._json({"origin": f"{o.lat},{o.lon}", "dest": f"{d.lat},{d.lon}",
                        "depart": dep.strftime("%Y-%m-%dT%H:%M")})
        elif path == "/api/plan":
            self._handle_plan(parse_qs(parsed.query))
        else:
            self._json({"error": "not found"}, 404)

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
                geocoder=None, default_depart: datetime | None = None,
                verbose: bool = False) -> ThreadingHTTPServer:
    """Build (but do not start) the demo server; defaults to the sample feed."""
    if timetable is None:
        timetable = sample_timetable()
        if default_depart is None:
            default_depart = sample_trip()[2]
    if default_depart is None:
        default_depart = datetime.now().replace(second=0, microsecond=0)
    server = ThreadingHTTPServer((host, port), _Handler)
    server.timetable = timetable
    server.region = region
    server.data_dir = data_dir
    server.turn_aware = turn_aware
    server.geocoder = geocoder
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
    args = parser.parse_args(argv)
    serve(args.host, args.port, region=args.region, data_dir=args.data_dir,
          turn_aware=args.turn_aware)


if __name__ == "__main__":
    main()
