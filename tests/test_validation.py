"""Coordinate validation: bad input raises clearly instead of silently failing."""

import pytest

from travelplanner import place, city
from travelplanner.models import Location, LocationType
from travelplanner.roads import _coerce


def test_location_rejects_out_of_range_lat():
    with pytest.raises(ValueError, match="latitude"):
        Location("bad", LocationType.CITY, 999.0, 9.0)


def test_location_rejects_out_of_range_lon():
    with pytest.raises(ValueError, match="longitude"):
        Location("bad", LocationType.CITY, 47.0, -500.0)


def test_location_accepts_extremes():
    Location("np", LocationType.LANDMARK, 90.0, 180.0)
    Location("sp", LocationType.LANDMARK, -90.0, -180.0)


def test_place_validates():
    with pytest.raises(ValueError):
        place("x", LocationType.HOTEL, 91.0, 0.0)


def test_coerce_out_of_range_coord_raises_clear_error():
    # "999,-500" parses as a coordinate -> clear range error, NOT a city lookup.
    with pytest.raises(ValueError, match="latitude|longitude"):
        _coerce("999,-500")


def test_coerce_valid_coord_and_city():
    loc = _coerce("47.1,9.5")
    assert loc.type is LocationType.LANDMARK
    assert (round(loc.lat, 1), round(loc.lon, 1)) == (47.1, 9.5)
    assert city("London").type is LocationType.CITY
