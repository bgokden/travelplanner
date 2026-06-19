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


def lookup_city(name: str) -> tuple[float, float] | None:
    """Coordinates for a bundled city name, or None if it is not in the table."""
    return _load_cities().get(name.strip().lower())
