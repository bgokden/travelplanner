"""Bundled reference data: airports and cities, with lookup helpers."""

import csv
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources

from travelplanner.geo import haversine
from travelplanner.models import Location, LocationType


@dataclass(frozen=True)
class Airport:
    iata: str
    name: str
    city: str
    country: str
    lat: float
    lon: float


@lru_cache(maxsize=1)
def _load_airports() -> list[Airport]:
    text = resources.files("travelplanner.data").joinpath("airports.csv").read_text(
        encoding="utf-8"
    )
    reader = csv.DictReader(text.splitlines())
    return [
        Airport(
            iata=row["iata"],
            name=row["name"],
            city=row["city"],
            country=row["country"],
            lat=float(row["lat"]),
            lon=float(row["lon"]),
        )
        for row in reader
    ]


@lru_cache(maxsize=1)
def _load_cities() -> dict[str, tuple[float, float]]:
    text = resources.files("travelplanner.data").joinpath("cities.csv").read_text(
        encoding="utf-8"
    )
    reader = csv.DictReader(text.splitlines())
    return {row["name"].strip().lower(): (float(row["lat"]), float(row["lon"]))
            for row in reader}


def nearest_airport(lat: float, lon: float) -> Airport:
    """Return the closest bundled airport to the given coordinates."""
    airports = _load_airports()
    return min(airports, key=lambda a: haversine(lat, lon, a.lat, a.lon))


def airport_to_location(airport: Airport) -> Location:
    return Location(
        name=f"{airport.city} ({airport.iata})",
        type=LocationType.AIRPORT,
        lat=airport.lat,
        lon=airport.lon,
    )


def resolve_city(name: str) -> tuple[float, float]:
    """Look up coordinates for a known city name. Raises if unknown."""
    coords = _load_cities().get(name.strip().lower())
    if coords is None:
        raise ValueError(
            f"Unknown city '{name}'. Provide lat/lon explicitly, "
            f"or use one of the bundled cities."
        )
    return coords
