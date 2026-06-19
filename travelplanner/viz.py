"""Render a calculated route to a self-contained HTML map (Leaflet via CDN).

    from travelplanner import drive_route
    from travelplanner.viz import save_route_map
    route = drive_route("Zaandam", "Schiphol")
    save_route_map(route, "route.html")

No Python dependencies; the produced HTML pulls Leaflet from a CDN and draws the
route polyline with start/end markers and a popup of distance + duration. Open it
in a browser. For a dependency-free data export use route.to_geojson().
"""

import json

_TEMPLATE = """<!doctype html><html><head><meta charset="utf-8">
<title>{title}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>html,body,#map{{height:100%;margin:0}}
.info{{position:absolute;z-index:1000;top:10px;left:10px;background:#fff;
 padding:8px 12px;border-radius:8px;font:14px system-ui;box-shadow:0 1px 6px rgba(0,0,0,.3)}}</style>
</head><body><div id="map"></div>
<div class="info">{label}</div>
<script>
const coords = {coords};        // [[lat, lon], ...]
const map = L.map('map');
L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',
  {{maxZoom:19, attribution:'&copy; OpenStreetMap'}}).addTo(map);
const line = L.polyline(coords, {{color:'#2b6cb0', weight:5, opacity:.8}}).addTo(map);
map.fitBounds(line.getBounds(), {{padding:[30,30]}});
L.marker(coords[0]).addTo(map).bindPopup('Start');
L.marker(coords[coords.length-1]).addTo(map).bindPopup('Destination');
</script></body></html>"""


def route_map_html(route, *, title: str = "Route") -> str:
    """Return a self-contained HTML document drawing the route on a map."""
    if not route.drivable or not route.geometry:
        raise ValueError("route is not drivable / has no geometry to draw")
    coords = [[lat, lon] for lat, lon in route.geometry]
    mins = route.duration.total_seconds() / 60 if route.duration else 0
    label = f"{route.distance_km} km &middot; {mins:.0f} min"
    return _TEMPLATE.format(title=title, label=label,
                            coords=json.dumps(coords))


def save_route_map(route, path: str, *, title: str = "Route") -> str:
    """Write the route map HTML to `path`; return the path."""
    with open(path, "w", encoding="utf-8") as f:
        f.write(route_map_html(route, title=title))
    return path
