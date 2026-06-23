"""Pluggable, approximate fare models: a representative cost per leg.

A fare model maps a leg's transport mode and distance to a representative monetary
amount in the model's currency:

    FareModel = (mode, distance_km) -> float        # amount, model currency

This is a deliberately ROUGH, distance-and-mode heuristic for ranking and a
ballpark "how much?", NOT a quoted ticket price. It ignores discounts, daily and
weekly caps, transfer rules, advance-purchase and yield pricing, peak/off-peak,
and travel class -- do not quote it. It exists to give CHEAPEST a continuous number
to order on (the alternative is the 3-level cost_level band) and to show a label.

The default is always on (like the per-km emissions factors used for GREENEST),
so an itinerary always carries an estimate; swap in `free_model` to ignore cost, or
any other FareModel (e.g. a data-backed backend) via set_fare_model.

The default per-mode rates are best-estimate Europe-centric EUR heuristics, meant
to be calibrated per deployment; the architecture does not depend on their values.
"""

from typing import Callable, Optional

from travelplanner.models import Mode

FareModel = Callable[[Mode, float], float]

DEFAULT_CURRENCY = "EUR"

# (base, per_km) in DEFAULT_CURRENCY: a flat per-leg/boarding charge plus a marginal
# distance rate. Rough Europe-centric figures (calibrated against ADAC fuel, rail
# corridor, and ferry surveys -- see tmp/FARE_MODEL_DESIGN.md); tunable.
#   CAR    marginal running cost (fuel ~0.12 + wear), not ownership/depreciation.
#   TRAIN  advance-weighted intercity per-km with a base; stands in for base-
#          dominated transit too.
#   FERRY  roughest of all -- per-km varies ~10x by route/length; central estimate.
#   FLIGHT base-heavy + low per-km to approximate fares flattening with distance.
FARE_RATES = {
    Mode.WALK:   (0.0, 0.0),
    Mode.CAR:    (0.0, 0.15),
    Mode.TRAIN:  (2.50, 0.12),
    Mode.FERRY:  (5.0, 0.15),
    Mode.FLIGHT: (45.0, 0.07),
}

# Unknown mode: price it like a car (a neutral motorised rate), never crash.
_UNKNOWN_RATE = (0.0, 0.20)


def heuristic_fare_model(rates: Optional[dict] = None,
                         currency: str = DEFAULT_CURRENCY) -> FareModel:
    """base + per_km * distance per mode (the default). Clamped to >= 0."""
    table = FARE_RATES if rates is None else rates

    def model(mode: Mode, distance_km: float) -> float:
        base, per_km = table.get(mode, _UNKNOWN_RATE)
        return max(0.0, base + per_km * distance_km)

    model.currency = currency
    return model


def free_model(mode: Mode, distance_km: float) -> float:
    """Always 0: opt out of fare estimation entirely."""
    return 0.0


free_model.currency = DEFAULT_CURRENCY


# The active default model. Always on, like the GREENEST emissions factors -- every
# itinerary carries an estimate unless the caller swaps in free_model.
_active: FareModel = heuristic_fare_model()


def set_fare_model(model: FareModel) -> None:
    """Set the active fare model used when legs are priced."""
    global _active
    _active = model


def get_fare_model() -> FareModel:
    """The active fare model (default: heuristic_fare_model())."""
    return _active


def reset_fare_model() -> None:
    """Restore the default heuristic fare model."""
    global _active
    _active = heuristic_fare_model()


def fare_currency() -> str:
    """Currency of the active fare model's amounts."""
    return getattr(_active, "currency", DEFAULT_CURRENCY)
