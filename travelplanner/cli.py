"""Command-line interface for travelplanner.

  travelplanner demo                          run the bundled multimodal sample
  travelplanner plan ORIGIN DEST [--gtfs DIR] plan a door-to-door trip

ORIGIN/DEST are either "lat,lon" or a bundled city name. With no --gtfs, the
bundled sample timetable is used.
"""

import argparse
from datetime import datetime

from travelplanner.models import Itinerary, LocationType
from travelplanner.graph.query import Objective


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


def _resolve_location(text: str):
    from travelplanner import city, place

    if "," in text:
        lat, lon = (float(x) for x in text.split(",", 1))
        return place(text, LocationType.LANDMARK, lat, lon)
    return city(text)


def _cmd_demo(_args) -> int:
    from travelplanner.graph.coupling import GeometricConnector, plan
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


def _cmd_plan(args) -> int:
    from travelplanner.graph.coupling import GeometricConnector, plan
    from travelplanner.graph.scheduled import load_timetable
    from travelplanner.samples import sample_timetable

    tt = load_timetable(args.gtfs) if args.gtfs else sample_timetable()
    conn = GeometricConnector(tt.stops)
    at = (datetime.fromisoformat(args.at) if args.at
          else datetime.now().replace(microsecond=0))
    try:
        origin = _resolve_location(args.origin)
        dest = _resolve_location(args.destination)
    except ValueError as exc:
        print(f"error: {exc}")
        return 2

    results = plan(origin, dest, at, tt, conn,
                   objective=Objective(args.objective))
    print(f"Plan: {origin.name} -> {dest.name}  departing {at:%Y-%m-%d %H:%M}"
          f"  [{args.objective}]\n")
    if not results:
        print("no itinerary found")
        return 0
    for rank, it in enumerate(results, 1):
        print(f"#{rank}")
        _print_itinerary(it)
        print()
    return 0


def _cmd_drive(args) -> int:
    from travelplanner.roads import drive

    try:
        result = drive(args.origin, args.destination, region=args.region)
    except (ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}")
        return 2
    if not result.drivable:
        print(f"{args.origin} -> {args.destination}: NOT drivable "
              f"(no road route in region {args.region!r})")
        return 0
    print(f"{args.origin} -> {args.destination} [{args.region}]: drivable, "
          f"{result.duration} ({result.distance_km} km)")
    return 0


def _cmd_prefetch(args) -> int:
    import os
    from travelplanner.roads import prefetch

    paths = prefetch(args.regions, build=args.build)
    for p in paths:
        size = os.path.getsize(p) / (1024 * 1024)
        print(f"cached {p} ({size:.0f} MB)")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="travelplanner",
        description="Multimodal travel planning that prioritizes air travel.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("demo", help="run the bundled multimodal sample")

    p = sub.add_parser("plan", help="plan a door-to-door trip")
    p.add_argument("origin", help='"lat,lon" or a bundled city name')
    p.add_argument("destination", help='"lat,lon" or a bundled city name')
    p.add_argument("--gtfs", help="GTFS feed directory (default: bundled sample)")
    p.add_argument("--at", help="departure time, ISO format (default: now)")
    p.add_argument("--objective", choices=[o.value for o in Objective],
                   default="air_priority", help="ranking objective")

    dr = sub.add_parser("drive",
                        help="street-accurate driving time + drivability")
    dr.add_argument("origin", help='"lat,lon" or a bundled city name')
    dr.add_argument("destination", help='"lat,lon" or a bundled city name')
    dr.add_argument("--region", required=True,
                    help="region name, Geofabrik URL, or local .osm.pbf path")

    pf = sub.add_parser("prefetch",
                        help="download region road data ahead of time")
    pf.add_argument("regions", nargs="+", help="one or more region names/URLs")
    pf.add_argument("--build", action="store_true",
                    help="also build the index to verify it")

    args = parser.parse_args(argv)
    if args.command == "demo":
        return _cmd_demo(args)
    if args.command == "plan":
        return _cmd_plan(args)
    if args.command == "drive":
        return _cmd_drive(args)
    if args.command == "prefetch":
        return _cmd_prefetch(args)
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
