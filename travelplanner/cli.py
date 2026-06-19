"""Command-line interface for travelplanner.

  travelplanner demo                 run the bundled multimodal sample
  travelplanner estimate A B [--at]  quick heuristic estimate between two cities
"""

import argparse
from datetime import datetime

from travelplanner.models import Itinerary, Mode


def _print_itinerary(it: Itinerary, indent: str = "    ") -> None:
    print(f"{indent}{it.primary_mode.value:6} arrive {it.arrive_at:%Y-%m-%d %H:%M}"
          f"  total {it.total_duration}  cost {it.cost_level.value}")
    clock = it.depart_at
    for leg in it.legs:
        clock = clock + leg.overhead
        dep = clock
        clock = clock + leg.travel_time
        wait = (f"  (+{int(leg.overhead.total_seconds() // 60)}m wait)"
                if leg.overhead.total_seconds() else "")
        print(f"{indent}  {leg.mode.value:6} {leg.from_loc.name} -> "
              f"{leg.to_loc.name}  {dep:%H:%M}-{clock:%H:%M}{wait}")


def _cmd_demo(_args) -> int:
    from travelplanner.graph.coupling import GeometricConnector, plan
    from travelplanner.graph.query import Objective
    from travelplanner.samples import sample_timetable, sample_trip

    tt = sample_timetable()
    conn = GeometricConnector(tt.stops)
    origin, dest, depart = sample_trip()
    print(f"Door-to-door: {origin.name} -> {dest.name}  "
          f"departing {depart:%Y-%m-%d %H:%M}\n")
    for objective in Objective:
        results = plan(origin, dest, depart, tt, conn, objective=objective)
        print(f"[{objective.value}]")
        if not results:
            print("    no itinerary")
        else:
            _print_itinerary(results[0])
        print()
    return 0


def _cmd_estimate(args) -> int:
    from travelplanner import city, estimate

    at = (datetime.fromisoformat(args.at) if args.at
          else datetime.now().replace(microsecond=0))
    try:
        origin, dest = city(args.origin), city(args.destination)
    except ValueError as exc:
        print(f"error: {exc}")
        return 2
    results = estimate(origin, dest, at)
    print(f"Estimate: {args.origin} -> {args.destination}  "
          f"departing {at:%Y-%m-%d %H:%M}\n")
    for rank, it in enumerate(results, 1):
        print(f"#{rank}")
        _print_itinerary(it)
        print()
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="travelplanner",
        description="Multimodal travel planning that prioritizes air travel.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("demo", help="run the bundled multimodal sample")

    est = sub.add_parser("estimate",
                         help="quick heuristic estimate between two cities")
    est.add_argument("origin", help="origin city name (bundled city table)")
    est.add_argument("destination", help="destination city name")
    est.add_argument("--at", help="departure time, ISO format "
                                  "(default: now)")

    args = parser.parse_args(argv)
    if args.command == "demo":
        return _cmd_demo(args)
    if args.command == "estimate":
        return _cmd_estimate(args)
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
