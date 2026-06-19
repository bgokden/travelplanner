"""Periodic API smoke + speed check.

Run this regularly (locally or in CI) to catch two kinds of regression:
  1. "does it still work"  -- the public API end-to-end on bundled/synthetic data
  2. "is it still fast"    -- latency stays within generous order-of-magnitude budgets

It is self-contained: no network and no large downloads. The road checks use a
small synthetic graph built in memory and a temporary offline artifact, so they
exercise build_region/road_router(data_dir)/drive/drive_matrix without OSM data.
They are skipped (not failed) if routingkit-cch is not installed.

Budgets are deliberately loose: the goal is to catch order-of-magnitude
regressions (e.g. a per-call rebuild creeping back in), not micro-noise. Exit
code is non-zero if any functional check fails or any budget is exceeded.

Usage:
    uv run --with routingkit-cch python bench/api_smoke.py
"""

import sys
import tempfile
import time
from datetime import date

from travelplanner import (
    Objective,
    drive,
    drive_matrix,
    itinerary_records,
    plan,
    road_router,
    sample_timetable,
    sample_trip,
)
from travelplanner.graph.coupling import GeometricConnector

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
_results: list[tuple[str, str, str]] = []


def _check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, PASS if ok else FAIL, detail))


def _budget(name: str, measured_s: float, budget_s: float) -> None:
    ok = measured_s <= budget_s
    _results.append((name, PASS if ok else FAIL,
                     f"{measured_s * 1000:.2f} ms (budget {budget_s * 1000:.0f} ms)"))


def check_planner() -> None:
    tt = sample_timetable()
    conn = GeometricConnector(tt.stops)
    origin, dest, depart = sample_trip()

    for obj in Objective:
        res = plan(origin, dest, depart, tt, conn, objective=obj)
        _check(f"plan/{obj.value} returns results", len(res) > 0)
    res = plan(origin, dest, depart, tt, conn)
    _check("itinerary.to_json is serializable", isinstance(res[0].to_json(), str))
    _check("itinerary_records produces rows", len(itinerary_records(res)) == len(res))

    t0 = time.perf_counter()
    for _ in range(50):
        plan(origin, dest, depart, tt, conn, objective=Objective.AIR_PRIORITY)
    _budget("plan() latency", (time.perf_counter() - t0) / 50, 0.005)


def check_roads() -> None:
    try:
        import routingkit_cch  # noqa: F401
    except ImportError:
        _results.append(("road checks", SKIP, "routingkit-cch not installed"))
        return

    from travelplanner.graph.road.model import RoadGraphBuilder
    from travelplanner.graph.road import CCHRoadRouter
    from travelplanner.graph.road.store import save_road_artifact

    b = RoadGraphBuilder(store_names=False)
    pts = {1: (47.10, 9.50), 2: (47.12, 9.52), 3: (47.15, 9.55), 4: (47.13, 9.49)}
    for k, (lat, lon) in pts.items():
        b.add_node(k, lat, lon)
    b.add_road(1, 2, 120)
    b.add_road(2, 3, 180)
    b.add_road(1, 4, 90)
    b.add_road(4, 3, 200)
    graph = b.build()

    data_dir = tempfile.mkdtemp()
    t0 = time.perf_counter()
    save_road_artifact(graph, CCHRoadRouter(graph).order, data_dir)
    build_s = time.perf_counter() - t0

    road_router.cache_clear()
    t0 = time.perf_counter()
    road_router("smoke", data_dir)  # cold load from artifact
    _budget("artifact cold load", time.perf_counter() - t0, 0.5)
    _check("build_region-style save", build_s < 5.0, f"{build_s * 1000:.0f} ms")

    coords = [pts[1], pts[3]]
    r = drive(coords[0], coords[1], region="smoke", data_dir=data_dir)
    _check("drive() drivable", r.drivable and r.duration is not None)

    # warm drive latency (the metric is cached after the first call)
    t0 = time.perf_counter()
    for _ in range(50):
        drive(coords[0], coords[1], region="smoke", data_dir=data_dir)
    _budget("drive() warm latency", (time.perf_counter() - t0) / 50, 0.05)

    m = drive_matrix(list(pts.values()), "smoke", data_dir=data_dir)
    _check("drive_matrix shape", len(m) == 4 and all(len(row) == 4 for row in m))
    _check("drive_matrix diagonal zero",
           all(m[i][i].distance_km == 0.0 for i in range(4)))


def main() -> int:
    check_planner()
    check_roads()

    width = max(len(n) for n, _, _ in _results)
    failed = 0
    for name, status, detail in _results:
        if status == FAIL:
            failed += 1
        print(f"  [{status}] {name.ljust(width)}  {detail}")
    print(f"\n{len(_results)} checks, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
