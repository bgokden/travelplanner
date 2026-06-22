"""Command-line interface for travelplanner.

  travelplanner demo                          run the bundled multimodal sample
  travelplanner plan ORIGIN DEST [--gtfs DIR] plan a door-to-door trip

ORIGIN/DEST are either "lat,lon" or a bundled city name. With no --gtfs, a
timetable is auto-composed for the trip (flights + GTFS feeds by location,
downloaded and cached on first use); pass --gtfs to use a specific feed, or run
'demo' for the offline bundled sample.
"""

import argparse
from datetime import datetime

from travelplanner.models import Itinerary, LocationType
from travelplanner.graph.query import Objective


def _in_zone(when, tz_name):
    """Show an aware datetime in a leg endpoint's local zone; pass naive times
    (a feed with no timezone data) through unchanged."""
    if tz_name and when.tzinfo is not None:
        from zoneinfo import ZoneInfo
        return when.astimezone(ZoneInfo(tz_name))
    return when


def _print_itinerary(it: Itinerary, indent: str = "    ") -> None:
    arrive = _in_zone(it.arrive_at, it.legs[-1].to_loc.tz if it.legs else None)
    print(f"{indent}{it.primary_mode.value:6} arrive {arrive:%Y-%m-%d %H:%M}"
          f"  total {it.total_duration}  cost {it.cost_level.value}")
    clock = it.depart_at
    for leg in it.legs:
        clock = clock + leg.overhead
        dep = _in_zone(clock, leg.from_loc.tz)
        clock = clock + leg.travel_time
        arr = _in_zone(clock, leg.to_loc.tz)
        wait = (f"  (+{int(leg.overhead.total_seconds() // 60)}m wait)"
                if leg.overhead.total_seconds() else "")
        print(f"{indent}  {leg.mode.value:6} {leg.from_loc.name} -> "
              f"{leg.to_loc.name}  {dep:%H:%M}-{arr:%H:%M}{wait}")


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
    import warnings
    from travelplanner.trips import plan_trip
    from travelplanner.graph.scheduled import load_timetable

    # No --gtfs: auto-compose a timetable for the trip (flights + GTFS by
    # location). An explicit --gtfs loads that feed instead.
    tt = load_timetable(args.gtfs) if args.gtfs else None
    at = (datetime.fromisoformat(args.at) if args.at
          else datetime.now().replace(microsecond=0))
    try:
        origin = _resolve_location(args.origin)
        dest = _resolve_location(args.destination)
    except ValueError as exc:
        print(f"error: {exc}")
        return 2

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        results = plan_trip(origin, dest, at, tt,
                            objective=Objective(args.objective))
    source = args.gtfs if args.gtfs else "auto (flights + GTFS by location)"
    print(f"Plan: {origin.name} -> {dest.name}  departing {at:%Y-%m-%d %H:%M}"
          f"  [{args.objective}]  data: {source}\n")
    for w in caught:
        print(f"note: {w.message}")
    if caught:
        print()
    if not results:
        print("no itinerary found")
        return 0
    for rank, it in enumerate(results, 1):
        print(f"#{rank}")
        _print_itinerary(it)
        print()
    return 0


def _cmd_transit_prefetch(args) -> int:
    from travelplanner.auto_timetable import build_default_timetable

    try:
        origin = _resolve_location(args.origin)
        dest = _resolve_location(args.destination)
    except ValueError as exc:
        print(f"error: {exc}")
        return 2
    # Building the auto timetable downloads and caches everything a later offline
    # 'plan' for this trip needs: the feed catalog, the covering GTFS feed(s), and
    # the OpenFlights data.
    tt, notes = build_default_timetable(origin, dest, download=True)
    print(f"prefetched transit data for {origin.name} -> {dest.name}: "
          f"{len(tt.stops)} stops, {len(tt.trips)} trips cached")
    for n in notes:
        print(f"note: {n}")
    return 0


def _cmd_drive(args) -> int:
    from travelplanner.roads import drive

    try:
        result = drive(args.origin, args.destination, region=args.region,
                       data_dir=args.data_dir)
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


def _cmd_build(args) -> int:
    from travelplanner.roads import build_region

    out = build_region(args.region, args.out_dir)
    print(f"built offline artifact for {args.region!r} in {out}")
    return 0


def _cmd_regions(args) -> int:
    from travelplanner.geofabrik import list_regions

    regions = list_regions()
    if args.filter:
        needle = args.filter.lower()
        regions = [r for r in regions
                   if needle in r.id.lower() or needle in r.name.lower()]
    for r in regions:
        parent = f" (in {r.parent})" if r.parent else ""
        print(f"{r.id}\t{r.name}{parent}")
    print(f"\n{len(regions)} regions")
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
    p.add_argument("--gtfs", help="GTFS feed directory (default: auto-compose "
                   "flights + GTFS for the trip; needs network on first use)")
    p.add_argument("--at", help="departure time, ISO format (default: now)")
    p.add_argument("--objective", choices=[o.value for o in Objective],
                   default="air_priority", help="ranking objective")

    tp = sub.add_parser("transit-prefetch",
                        help="download a trip's transit data (catalog + GTFS "
                             "feeds + flights) ahead of an offline 'plan'")
    tp.add_argument("origin", help='"lat,lon" or a bundled city name')
    tp.add_argument("destination", help='"lat,lon" or a bundled city name')

    dr = sub.add_parser("drive",
                        help="street-accurate driving time + drivability")
    dr.add_argument("origin", help='"lat,lon" or a bundled city name')
    dr.add_argument("destination", help='"lat,lon" or a bundled city name')
    dr.add_argument("--region", required=True,
                    help="region name, Geofabrik URL, or local .osm.pbf path")
    dr.add_argument("--data-dir",
                    help="prebuilt offline artifact dir (see 'build'); "
                         "skips download and rebuild")

    pf = sub.add_parser("prefetch",
                        help="download region road data ahead of time")
    pf.add_argument("regions", nargs="+", help="one or more region names/URLs")
    pf.add_argument("--build", action="store_true",
                    help="also build the index to verify it")

    bd = sub.add_parser("build",
                        help="build an offline road artifact (run at build time)")
    bd.add_argument("region", help="region name, Geofabrik URL, or local .osm.pbf path")
    bd.add_argument("--out-dir", required=True,
                    help="directory to write the offline artifact into")

    rg = sub.add_parser("regions",
                        help="list downloadable Geofabrik regions")
    rg.add_argument("--filter", help="only show regions matching this substring")

    args = parser.parse_args(argv)
    if args.command == "demo":
        return _cmd_demo(args)
    if args.command == "plan":
        return _cmd_plan(args)
    if args.command == "transit-prefetch":
        return _cmd_transit_prefetch(args)
    if args.command == "drive":
        return _cmd_drive(args)
    if args.command == "prefetch":
        return _cmd_prefetch(args)
    if args.command == "build":
        return _cmd_build(args)
    if args.command == "regions":
        return _cmd_regions(args)
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
