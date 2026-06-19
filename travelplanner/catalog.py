"""Bundled reference data: a small city table with a name lookup."""

import csv
from functools import lru_cache
from importlib import resources


@lru_cache(maxsize=1)
def _load_cities() -> dict[str, tuple[float, float]]:
    text = resources.files("travelplanner.data").joinpath("cities.csv").read_text(
        encoding="utf-8"
    )
    reader = csv.DictReader(text.splitlines())
    return {row["name"].strip().lower(): (float(row["lat"]), float(row["lon"]))
            for row in reader}


def resolve_city(name: str) -> tuple[float, float]:
    """Look up coordinates for a known city name. Raises if unknown."""
    coords = _load_cities().get(name.strip().lower())
    if coords is None:
        raise ValueError(
            f"Unknown city '{name}'. Provide lat/lon explicitly, "
            f"or use one of the bundled cities."
        )
    return coords
