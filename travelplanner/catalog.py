"""Bundled reference data: a small city table with name lookup + search."""

import csv
from functools import lru_cache
from importlib import resources


@lru_cache(maxsize=1)
def _load_city_rows() -> tuple[dict, ...]:
    text = resources.files("travelplanner.data").joinpath("cities.csv").read_text(
        encoding="utf-8"
    )
    reader = csv.DictReader(text.splitlines())
    return tuple({"name": row["name"].strip(),
                  "country": row.get("country", "").strip(),
                  "lat": float(row["lat"]), "lon": float(row["lon"])}
                 for row in reader)


@lru_cache(maxsize=1)
def _load_cities() -> dict[str, tuple[float, float]]:
    return {row["name"].lower(): (row["lat"], row["lon"])
            for row in _load_city_rows()}


def lookup_city(name: str) -> tuple[float, float] | None:
    """Coordinates for a bundled city name, or None if it is not in the table."""
    return _load_cities().get(name.strip().lower())


def search_cities(query: str, *, limit: int = 8) -> list[dict]:
    """Bundled cities whose name matches `query` (case-insensitive).

    Returns up to `limit` rows ({name, country, lat, lon}); names that start with
    the query rank before names that merely contain it, then alphabetically. An
    empty/short query returns nothing (nothing to suggest yet).
    """
    q = query.strip().lower()
    if not q:
        return []
    starts, contains = [], []
    for row in _load_city_rows():
        name = row["name"].lower()
        if name.startswith(q):
            starts.append(row)
        elif q in name:
            contains.append(row)
    starts.sort(key=lambda r: r["name"].lower())
    contains.sort(key=lambda r: r["name"].lower())
    return [dict(r) for r in (starts + contains)[:limit]]
