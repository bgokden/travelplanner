"""Geofabrik extract catalog: discover every downloadable region.

Geofabrik publishes its full extract list as JSON. `index-v1-nogeom.json` lists
~555 regions (id, name, parent, and the .osm.pbf URL) without boundary geometry,
which is all that is needed to enumerate regions and resolve a name to a URL.
The index is downloaded once and cached on disk.

    list_regions()            -> every Region (downloads the index if needed)
    catalog()                 -> {id: Region}
    cached_catalog()          -> {id: Region}, or {} if the index isn't cached
                                 (never downloads; safe at runtime/offline)

Coordinate-based selection (which region contains a point) needs the larger
geometry index and is a separate concern.
"""

import json
import os
import urllib.request
from dataclasses import dataclass

INDEX_URL = "https://download.geofabrik.de/index-v1-nogeom.json"
_INDEX_FILE = "geofabrik-index-nogeom.json"


@dataclass(frozen=True)
class Region:
    id: str
    name: str
    parent: str | None
    pbf_url: str


def _index_path() -> str:
    from travelplanner.roads import cache_dir
    return os.path.join(cache_dir(), _INDEX_FILE)


def _download_index(dest: str) -> None:
    tmp = dest + ".part"
    req = urllib.request.Request(INDEX_URL, headers={"User-Agent": "travelplanner"})
    with urllib.request.urlopen(req) as resp, open(tmp, "wb") as out:
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
    os.replace(tmp, dest)


def _parse_catalog(data: dict) -> dict[str, Region]:
    out: dict[str, Region] = {}
    for feature in data.get("features", []):
        props = feature.get("properties", {})
        pbf = (props.get("urls") or {}).get("pbf")
        region_id = props.get("id")
        if not pbf or not region_id:
            continue
        out[region_id] = Region(region_id, props.get("name", region_id),
                                 props.get("parent"), pbf)
    return out


def catalog(*, refresh: bool = False) -> dict[str, Region]:
    """The full catalog, downloading + caching the index if needed."""
    path = _index_path()
    if refresh or not os.path.exists(path):
        _download_index(path)
    with open(path, encoding="utf-8") as f:
        return _parse_catalog(json.load(f))


def cached_catalog() -> dict[str, Region]:
    """The catalog if the index is already cached, else {} (never downloads)."""
    path = _index_path()
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return _parse_catalog(json.load(f))


def list_regions(*, refresh: bool = False) -> list[Region]:
    """Every downloadable region, sorted by id (downloads the index if needed)."""
    return sorted(catalog(refresh=refresh).values(), key=lambda r: r.id)
