"""Approximate fare model: per-mode arithmetic, fallback, currency, active model."""

from travelplanner.models import Mode
from travelplanner.fares import (
    DEFAULT_CURRENCY,
    fare_currency,
    free_model,
    get_fare_model,
    heuristic_fare_model,
    reset_fare_model,
    set_fare_model,
)


def test_heuristic_base_plus_per_km():
    m = heuristic_fare_model()
    assert m(Mode.WALK, 5.0) == 0.0
    assert m(Mode.CAR, 100.0) == 20.0            # 0 + 0.20 * 100
    assert m(Mode.TRAIN, 100.0) == 2.50 + 0.15 * 100
    assert m(Mode.FLIGHT, 500.0) == 45.0 + 0.07 * 500


def test_unknown_mode_priced_like_car():
    # An empty rate table exercises the unknown-mode fallback (car-like, never crash).
    m = heuristic_fare_model(rates={})
    assert m(Mode.TRAIN, 100.0) == 20.0          # _UNKNOWN_RATE = (0, 0.20)


def test_amount_clamped_non_negative():
    m = heuristic_fare_model(rates={Mode.CAR: (-5.0, 0.0)})
    assert m(Mode.CAR, 10.0) == 0.0              # max(0, -5)


def test_free_model_is_zero():
    assert free_model(Mode.FLIGHT, 9999.0) == 0.0


def test_currency_default_and_override():
    assert heuristic_fare_model().currency == DEFAULT_CURRENCY
    assert heuristic_fare_model(currency="USD").currency == "USD"
    assert free_model.currency == DEFAULT_CURRENCY


def test_active_model_set_get_reset():
    try:
        assert get_fare_model()(Mode.CAR, 10.0) == 2.0    # default heuristic
        assert fare_currency() == DEFAULT_CURRENCY
        set_fare_model(free_model)
        assert get_fare_model()(Mode.CAR, 10.0) == 0.0
        usd = heuristic_fare_model(currency="USD")
        set_fare_model(usd)
        assert fare_currency() == "USD"
    finally:
        reset_fare_model()
    assert get_fare_model()(Mode.CAR, 10.0) == 2.0
    assert fare_currency() == DEFAULT_CURRENCY
