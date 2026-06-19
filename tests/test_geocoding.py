"""Pluggable geocoding: bundled, chain, cache, nominatim (mocked), active config."""

import io
import json

import pytest

from travelplanner import city, geocode as tp
from travelplanner import set_geocoder, reset_geocoder
from travelplanner.geocoding import (
    bundled_geocoder,
    cached,
    chain,
    nominatim_geocoder,
    resolve_city,
)
from travelplanner.roads import _coerce


@pytest.fixture(autouse=True)
def _restore_active():
    yield
    reset_geocoder()


def test_bundled_hits_and_misses():
    assert bundled_geocoder("Berlin") is not None
    assert bundled_geocoder("Nowhereville") is None


def test_chain_first_non_none_wins():
    g = chain(lambda n: None, lambda n: (1.0, 2.0), lambda n: (9.0, 9.0))
    assert g("anything") == (1.0, 2.0)
    assert chain(lambda n: None)("x") is None


def test_cached_persists_and_short_circuits(tmp_path):
    calls = []

    def inner(name):
        calls.append(name)
        return (47.0, 9.0) if name == "Vaduz" else None

    path = str(tmp_path / "geo.json")
    g = cached(inner, path=path)
    assert g("Vaduz") == (47.0, 9.0)
    assert g("Vaduz") == (47.0, 9.0)        # served from cache
    assert calls == ["Vaduz"]               # inner called once only
    assert g("Unknown") is None             # miss not cached
    assert calls == ["Vaduz", "Unknown"]

    # persisted to disk and readable by a fresh cache over the same file
    on_disk = json.loads((tmp_path / "geo.json").read_text())
    assert on_disk["vaduz"] == [47.0, 9.0]
    fresh = cached(lambda n: pytest.fail("should not call inner"), path=path)
    assert fresh("Vaduz") == (47.0, 9.0)


def test_resolve_city_uses_active_and_override():
    set_geocoder(lambda n: (10.0, 20.0))
    assert resolve_city("anything") == (10.0, 20.0)
    # explicit geocoder overrides the active one
    assert resolve_city("x", geocoder=lambda n: (1.0, 1.0)) == (1.0, 1.0)


def test_resolve_city_raises_when_unresolved():
    set_geocoder(lambda n: None)
    with pytest.raises(ValueError, match="Could not resolve"):
        resolve_city("ghost town")


def test_city_and_coerce_honor_custom_geocoder():
    g = lambda n: (51.5, -0.12) if n == "MyPlace" else None
    loc = city("MyPlace", geocoder=g)
    assert (round(loc.lat, 1), round(loc.lon, 1)) == (51.5, -0.1)
    coerced = _coerce("MyPlace", geocoder=g)
    assert (round(coerced.lat, 1), round(coerced.lon, 1)) == (51.5, -0.1)


def test_nominatim_parses_response(monkeypatch):
    payload = json.dumps([{"lat": "48.8566", "lon": "2.3522"}]).encode()

    class _Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr("urllib.request.urlopen",
                        lambda req, timeout=None: _Resp(payload))
    g = nominatim_geocoder(user_agent="test")
    assert g("Paris") == (48.8566, 2.3522)


def test_nominatim_network_error_returns_none(monkeypatch):
    import urllib.error

    def boom(req, timeout=None):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    g = nominatim_geocoder(user_agent="test")
    assert g("Paris") is None             # offline-safe: degrades to None


def test_top_level_geocode_uses_active():
    set_geocoder(lambda n: (5.0, 6.0))
    assert tp("anything") == (5.0, 6.0)
