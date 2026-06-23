"""Render calculated routes onto a self-contained HTML map (Leaflet via CDN).

    from travelplanner import drive_route
    from travelplanner.viz import save_route_map, save_routes_map

    save_route_map(drive_route("Zaandam", "Schiphol"), "route.html")

    # overlay several routes with a legend (e.g. node-based vs turn-aware):
    save_routes_map([(r_fast, "fastest", "#2b6cb0"),
                     (r_turn, "turn-aware", "#dd6b20")], "compare.html")

No Python dependencies; the HTML pulls Leaflet from a CDN, draws each route's
polyline with start/end markers, and shows a legend of distance + duration per
layer. For a dependency-free data export use route.to_geojson().
"""

import json

_TEMPLATE = """<!doctype html><html><head><meta charset="utf-8">
<title>{title}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>html,body,#map{{height:100%;margin:0}}
.legend{{position:absolute;z-index:1000;top:10px;left:10px;background:#fff;
 padding:8px 12px;border-radius:8px;font:13px/1.5 system-ui;
 box-shadow:0 1px 6px rgba(0,0,0,.3)}}
.legend b{{font-size:14px}} .sw{{display:inline-block;width:12px;height:12px;
 border-radius:2px;margin-right:6px;vertical-align:middle}}</style>
</head><body><div id="map"></div>
<div class="legend"><b>{title}</b><br>{legend}</div>
<script>
const layers = {layers};   // [{{coords:[[lat,lon]..], color, label}}]
const map = L.map('map');
L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',
  {{maxZoom:19, attribution:'&copy; OpenStreetMap'}}).addTo(map);
const group = [];
layers.forEach(l => {{
  const line = L.polyline(l.coords, {{color:l.color, weight:5, opacity:.8}}).addTo(map);
  line.bindPopup(l.label);
  group.push(line);
}});
const first = layers[0].coords;
const lastCoords = layers[layers.length-1].coords;
const last0 = lastCoords[lastCoords.length-1];
L.marker(first[0]).addTo(map).bindPopup('Start');
L.marker(last0).addTo(map).bindPopup('Destination');
map.fitBounds(L.featureGroup(group).getBounds(), {{padding:[30,30]}});
</script></body></html>"""


def _layer(route, label, color) -> dict:
    if not route.drivable or not route.geometry:
        raise ValueError(f"route {label!r} is not drivable / has no geometry")
    return {"coords": [[lat, lon] for lat, lon in route.geometry],
            "color": color, "label": label}


def routes_map_html(layers, *, title: str = "Routes") -> str:
    """Self-contained HTML overlaying several routes. layers = [(route, label, color)]."""
    segments = []
    for route, label, color in layers:
        mins = route.duration.total_seconds() / 60 if route.duration else 0
        segments.append({
            "coords": _layer(route, label, color)["coords"],   # validates geometry
            "color": color,
            "label": f"{label}: {route.distance_km} km &middot; {mins:.0f} min",
        })
    return segments_map_html(segments, title=title)


def save_routes_map(layers, path: str, *, title: str = "Routes") -> str:
    """Write an overlay of several routes to `path`; return the path."""
    with open(path, "w", encoding="utf-8") as f:
        f.write(routes_map_html(layers, title=title))
    return path


def route_map_html(route, *, title: str = "Route") -> str:
    """Self-contained HTML drawing a single route on a map."""
    return routes_map_html([(route, title, "#2b6cb0")], title=title)


def save_route_map(route, path: str, *, title: str = "Route") -> str:
    """Write a single-route map to `path`; return the path."""
    return save_routes_map([(route, title, "#2b6cb0")], path, title=title)


# Per-mode colours for a multimodal itinerary (walk/car access + line-haul).
MODE_COLORS = {
    "walk": "#718096",
    "car": "#2b6cb0",
    "train": "#2f855a",
    "ferry": "#319795",
    "flight": "#dd6b20",
}


def segments_map_html(segments, *, title: str = "Trip", header: str = "") -> str:
    """Self-contained HTML drawing coloured polylines for arbitrary segments.

    `segments` is a list of {"coords": [[lat, lon], ...], "color": "#hex",
    "label": str}. `header` is an optional line shown above the per-segment
    legend (e.g. a trip summary). This is the generic renderer the itinerary and
    route maps build on; pass real routed geometry in `coords` for an accurate
    overlay, or two endpoints for a straight segment.
    """
    if not segments:
        raise ValueError("no segments to draw")
    if any(not s["coords"] for s in segments):
        raise ValueError("every segment needs at least one coordinate")
    rows = [f'<span class="sw" style="background:{s["color"]}"></span>{s["label"]}'
            for s in segments]
    legend = (f"{header}<br>" if header else "") + "<br>".join(rows)
    layers = [{"coords": s["coords"], "color": s["color"], "label": s["label"]}
              for s in segments]
    return _TEMPLATE.format(title=title, legend=legend, layers=json.dumps(layers))


def save_segments_map(segments, path: str, *, title: str = "Trip",
                      header: str = "") -> str:
    """Write a coloured-polyline overlay of `segments` to `path`; return the path."""
    with open(path, "w", encoding="utf-8") as f:
        f.write(segments_map_html(segments, title=title, header=header))
    return path


def _itinerary_segments(itinerary, geometries=None) -> list:
    """One coloured segment per leg, coloured by mode.

    By default a leg is a straight line between its endpoints (a leg only knows
    its from/to coordinates). Pass `geometries` -- a dict mapping a 1-based leg
    index to a list of (lat, lon) points -- to draw that leg along its real
    routed path instead (e.g. road geometry from `drive_route`).
    """
    geometries = geometries or {}
    segments = []
    for i, leg in enumerate(itinerary.legs, 1):
        mode = leg.mode.value
        # Prefer an explicit override (geometries dict), then the leg's own routed
        # polyline (set by a road-backed connector), else the straight endpoints. A
        # real polyline needs >= 2 points; a shorter one falls back rather than
        # drawing an invisible segment.
        if i in geometries and len(geometries[i]) >= 2:
            coords = [[lat, lon] for lat, lon in geometries[i]]
        elif leg.geometry and len(leg.geometry) >= 2:
            coords = [[lat, lon] for lat, lon in leg.geometry]
        else:
            coords = [[leg.from_loc.lat, leg.from_loc.lon],
                      [leg.to_loc.lat, leg.to_loc.lon]]
        mins = leg.duration.total_seconds() / 60
        label = (f"{i}. {mode}: {leg.from_loc.name} &rarr; {leg.to_loc.name} "
                 f"({leg.distance_km:.0f} km &middot; {mins:.0f} min)")
        segments.append({"coords": coords, "mode": mode,
                         "color": MODE_COLORS.get(mode, "#000000"),
                         "label": label})
    return segments


def itinerary_segments(itinerary, geometries=None) -> list:
    """Public per-leg map segments ({coords, mode, color, label}) for an itinerary.

    The same data the itinerary map draws, exposed for callers (e.g. the demo
    service API) that render the legs themselves. Pass `geometries` (1-based leg
    index -> routed (lat, lon) path) to follow real road geometry on those legs.
    """
    return _itinerary_segments(itinerary, geometries)


def itinerary_map_html(itinerary, *, title: str = "Trip", geometries=None) -> str:
    """Self-contained HTML drawing one door-to-door itinerary's legs by mode.

    `geometries` optionally maps a 1-based leg index to its real routed (lat, lon)
    path so road legs follow the streets; legs without geometry are straight.
    """
    if not itinerary.legs:
        raise ValueError("itinerary has no legs to draw")
    arrive = itinerary.arrive_at.strftime("%H:%M")
    header = (f'{itinerary.depart_at.strftime("%a %H:%M")} &rarr; {arrive} '
              f'&middot; {itinerary.total_minutes:.0f} min')
    return segments_map_html(_itinerary_segments(itinerary, geometries),
                             title=title, header=header)


def save_itinerary_map(itinerary, path: str, *, title: str = "Trip",
                       geometries=None) -> str:
    """Write a multimodal itinerary map to `path`; return the path."""
    with open(path, "w", encoding="utf-8") as f:
        f.write(itinerary_map_html(itinerary, title=title, geometries=geometries))
    return path
