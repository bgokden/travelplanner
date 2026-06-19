"""Pluggable driving-speed models: free-flow, average, and time-of-day.

A speed model maps a road's highway class and the departure time to a TIME
MULTIPLIER on its free-flow travel time (the time implied by the speed limit):

    SpeedModel = (highway_class | None, depart_at | None) -> float
    1.0 = free-flow (best case);  >1.0 = slower than free-flow.

Arcs keep their free-flow `base_seconds`; the model is applied at customization,
once per distinct highway class, so the same road artifact serves any profile and
any departure time with no rebuild. The default decides automatically: it is
time-of-day aware, behaving as plain average when no departure time is given and
applying a rush-hour curve when one is -- the caller never selects a model, and
free-flow (the dangerous best case) is opt-in only.

These defaults are typical-day heuristics, not live traffic: they model "a normal
Tuesday at 8am", not today's accident. A data-backed model (returning
free_flow_time / predicted_time per class) is just another SpeedModel.
"""

from datetime import datetime
from typing import Callable, Optional

SpeedModel = Callable[[Optional[str], Optional[datetime]], float]

# Highway classes that flow near the limit (vs urban/arterial streets).
HIGHWAY_CLASSES = frozenset({"motorway", "motorway_link", "trunk", "trunk_link"})

# Average time multiplier vs free-flow per class (1 / typical achieved fraction).
AVERAGE_FACTORS = {
    "motorway": 1.05, "motorway_link": 1.10,
    "trunk": 1.11, "trunk_link": 1.18,
    "primary": 1.33, "primary_link": 1.40,
    "secondary": 1.43, "secondary_link": 1.50,
    "tertiary": 1.54, "tertiary_link": 1.60,
    "unclassified": 1.67, "residential": 1.82, "living_street": 2.00,
    "service": 2.00, "road": 1.54, "track": 2.00,
}


def free_flow_model(highway: Optional[str], depart_at: Optional[datetime]) -> float:
    """Best case: travel at the speed limit (multiplier 1.0). Opt-in."""
    return 1.0


def average_model(factors: Optional[dict] = None) -> SpeedModel:
    """Constant per-class slowdown reflecting typical conditions (the default)."""
    table = AVERAGE_FACTORS if factors is None else factors

    def model(highway: Optional[str], depart_at: Optional[datetime]) -> float:
        return table.get(highway or "", 1.0)
    return model


def _time_of_day_factor(highway: Optional[str], dt: datetime, *,
                        peak_urban: float, peak_highway: float,
                        night: float) -> float:
    is_highway = highway in HIGHWAY_CLASSES
    hour = dt.hour
    if dt.weekday() < 5:  # weekday
        if hour in (7, 8, 16, 17, 18):                 # rush hours
            return peak_highway if is_highway else peak_urban
        if hour >= 22 or hour <= 5:                     # night
            return night
        return 1.0
    # weekend: a mild midday bump, quiet nights
    if 11 <= hour <= 17:
        return 1.05 if is_highway else 1.15
    if hour >= 22 or hour <= 6:
        return night
    return 1.0


def time_of_day_model(base: Optional[SpeedModel] = None, *,
                      peak_urban: float = 1.45, peak_highway: float = 1.20,
                      night: float = 0.95) -> SpeedModel:
    """Average (or `base`) scaled by a heuristic hour/weekday congestion curve.

    The combined multiplier is clamped to >= 1.0 so a quiet hour never beats the
    free-flow speed limit. With no depart_at it falls back to `base`.
    """
    base_model = base if base is not None else average_model()

    def model(highway: Optional[str], depart_at: Optional[datetime]) -> float:
        b = base_model(highway, depart_at)
        if depart_at is None:
            return b
        factor = _time_of_day_factor(highway, depart_at, peak_urban=peak_urban,
                                     peak_highway=peak_highway, night=night)
        return max(1.0, b * factor)
    return model


# Default: time-of-day aware. It decides automatically -- with no departure time
# it behaves as plain average; given a depart_at it applies the rush-hour curve.
# The caller never has to choose a model.
_active: SpeedModel = time_of_day_model()


def set_speed_model(model: SpeedModel) -> None:
    """Set the active speed model used by drive()/customized() by default."""
    global _active
    _active = model


def get_speed_model() -> SpeedModel:
    """The active speed model (default: time_of_day_model())."""
    return _active


def reset_speed_model() -> None:
    """Restore the default time-of-day speed model."""
    global _active
    _active = time_of_day_model()
