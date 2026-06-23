"""Command-line interface for travelplanner.

  travelplanner demo                          run the bundled multimodal sample
  travelplanner plan ORIGIN DEST [--gtfs DIR] plan a door-to-door trip
  travelplanner attribution [ORIGIN DEST]     show data sources and licenses

ORIGIN/DEST are either "lat,lon" or a bundled city name. With no --gtfs, a
timetable is auto-composed for the trip (flights + GTFS feeds by location,
downloaded and cached on first use); pass --gtfs to use a specific feed, or run
'demo' for the offline bundled sample.
"""

import argparse
from datetime import datetime

from travelplanner.models import Itinerary, LocationType, humanize_duration
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
          f"  total {humanize_duration(it.total_duration)}  cost {it.cost_level.value}")
    for leg in it.legs:
        # The Itinerary stamped each leg's absolute clock; render in local time.
        dep = _in_zone(leg.depart_at, leg.from_loc.tz)
        arr = _in_zone(leg.arrive_at, leg.to_loc.tz)
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
    from travelplanner.graph.scheduled import (
        load_timetable, load_timetable_artifact)
    from travelplanner.trips import plan_trip

    # Timetable source, in precedence order: a prebuilt artifact (--timetable),
    # a specific GTFS feed (--gtfs), or auto-compose for the trip (neither).
    if args.timetable:
        tt = load_timetable_artifact(args.timetable)
        source = f"artifact {args.timetable}"
    elif args.gtfs:
        tt = load_timetable(args.gtfs)
        source = args.gtfs
    else:
        tt = None
        source = "auto (flights + GTFS by location)"
    at = (datetime.fromisoformat(args.at) if args.at
          else datetime.now().replace(microsecond=0))
    try:
        origin = _resolve_location(args.origin)
        dest = _resolve_location(args.destination)
    except ValueError as exc:
        print(f"error: {exc}")
        return 2

    if tt is None:
        # Auto-compose downloads data on a cold cache; say so before the stall so
        # it does not look like a hang (the captured warnings only print after).
        import sys
        print("composing a timetable for this trip "
              "(first run downloads data, then cached)...", file=sys.stderr)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        results = plan_trip(origin, dest, at, tt,
                            objective=Objective(args.objective), top_n=args.top)
    print(f"Plan: {origin.name} -> {dest.name}  departing {at:%Y-%m-%d %H:%M}"
          f"  [{args.objective}]  data: {source}\n")
    for w in caught:
        print(f"note: {w.message}")
    if caught:
        print()
    if not results:
        print("no itinerary found")
    else:
        for rank, it in enumerate(results, 1):
            print(f"#{rank}")
            _print_itinerary(it)
            print()
    # Auto-sourced and prebuilt-artifact timetables both embed third-party data
    # (OpenFlights/GTFS), so credit it; a user-supplied --gtfs feed is the caller's
    # own data and is not re-credited. Credit only the datasets these results
    # actually used, from the leg modes (a flight-only result must not credit GTFS).
    # CLI access/egress is straight-line, not OSM, so road=False here.
    if results and (tt is None or args.timetable):
        from travelplanner.attribution import data_sources
        from travelplanner.models import Mode
        modes = {leg.mode for it in results for leg in it.legs}
        ground = bool({Mode.TRAIN, Mode.FERRY} & modes)
        sources = data_sources(air=Mode.FLIGHT in modes, ground=ground, road=False)
        if sources:
            print("credits (data used in these results):")
            for a in sources:
                print(f"  {a.line()}")
            # The specific feeds are only recoverable for an auto-sourced trip.
            if ground and tt is None:
                print('  per-feed details: travelplanner attribution '
                      f'"{args.origin}" "{args.destination}"')
    return 0


def _cmd_attribution(args) -> int:
    from travelplanner.attribution import data_sources, render

    feeds = []
    if args.origin and args.destination:
        from travelplanner.transit_catalog import (
            cached_catalog, catalog, feeds_for_trip)
        try:
            origin = _resolve_location(args.origin)
            dest = _resolve_location(args.destination)
        except ValueError as exc:
            print(f"error: {exc}")
            return 2
        cat = cached_catalog()
        if not cat:
            try:
                print("note: downloading the feed catalog (first use)\n")
                cat = catalog()
            except OSError as exc:
                print(f"note: catalog unavailable ({exc}); "
                      "showing the general data sources only\n")
                cat = {}
        if cat:
            covering = feeds_for_trip((origin.lat, origin.lon),
                                      (dest.lat, dest.lon), catalog=cat)
            if not covering:
                print("note: no catalog feed covers this trip\n")
            else:
                # The planner downloads only the smallest-area covering feed, so
                # only that one is credited; the rest are unused alternatives.
                feeds = covering[:1]
                if len(covering) > 1:
                    alts = ", ".join(f.provider or f.name or f.id
                                     for f in covering[1:])
                    print("note: other feeds cover this trip but are not used: "
                          f"{alts}\n")
    elif args.origin or args.destination:
        print("note: give both origin and destination for per-feed licenses\n")
    print("Data sources travelplanner auto-fetches (credit them when you use or "
          "redistribute the data):\n")
    print(render(data_sources(feeds=feeds)))
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


def _cmd_transit_build(args) -> int:
    from travelplanner.attribution import data_sources, render
    from travelplanner.auto_timetable import build_default_timetable
    from travelplanner.graph.scheduled import save_timetable

    try:
        origin = _resolve_location(args.origin)
        dest = _resolve_location(args.destination)
    except ValueError as exc:
        print(f"error: {exc}")
        return 2
    # Compose the trip's timetable once (online) and serialize it, so a later
    # offline 'plan --timetable' loads it directly without re-downloading.
    tt, notes = build_default_timetable(origin, dest, download=True)
    save_timetable(tt, args.out)
    print(f"built timetable artifact for {origin.name} -> {dest.name}: "
          f"{len(tt.stops)} stops, {len(tt.trips)} trips -> {args.out}")
    for n in notes:
        print(f"note: {n}")
    # The artifact embeds OpenFlights/GTFS data; surface the credit at build time.
    print("\ndata sources (credit when you use or redistribute the artifact):")
    print(render(data_sources(road=False)))
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
    else:
        print(f"{args.origin} -> {args.destination} [{args.region}]: drivable, "
              f"{result.duration} ({result.distance_km} km)")
    print("data: OpenStreetMap contributors via Geofabrik, under ODbL "
          "(https://www.openstreetmap.org/copyright)")
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
        description="Multimodal, door-to-door travel planning.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("demo", help="run the bundled multimodal sample")

    p = sub.add_parser("plan", help="plan a door-to-door trip")
    p.add_argument("origin", help='"lat,lon" or a bundled city name')
    p.add_argument("destination", help='"lat,lon" or a bundled city name')
    p.add_argument("--gtfs", help="GTFS feed directory (default: auto-compose "
                   "flights + GTFS for the trip; needs network on first use)")
    p.add_argument("--timetable", help="prebuilt timetable artifact file (see "
                   "'transit-build'); loads offline, overrides --gtfs/auto")
    p.add_argument("--at", help="departure time, ISO format (default: now)")
    p.add_argument("--objective", choices=[o.value for o in Objective],
                   default="fastest", help="ranking objective (default: fastest)")
    p.add_argument("--top", type=int, default=3, metavar="N",
                   help="number of ranked routes to show (default: 3)")

    tp = sub.add_parser("transit-prefetch",
                        help="download a trip's transit data (catalog + GTFS "
                             "feeds + flights) ahead of an offline 'plan'")
    tp.add_argument("origin", help='"lat,lon" or a bundled city name')
    tp.add_argument("destination", help='"lat,lon" or a bundled city name')

    tb = sub.add_parser("transit-build",
                        help="compose a trip's timetable and save it as an "
                             "offline artifact (load with 'plan --timetable')")
    tb.add_argument("origin", help='"lat,lon" or a bundled city name')
    tb.add_argument("destination", help='"lat,lon" or a bundled city name')
    tb.add_argument("out", help="output artifact file (JSON)")

    ab = sub.add_parser("attribution",
                        help="show the data sources and licenses for "
                             "auto-sourced trips (OpenFlights, GTFS feeds)")
    ab.add_argument("origin", nargs="?",
                    help='optional "lat,lon" or city, to list per-feed licenses')
    ab.add_argument("destination", nargs="?",
                    help='optional "lat,lon" or city, to list per-feed licenses')

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
    if args.command == "transit-build":
        return _cmd_transit_build(args)
    if args.command == "attribution":
        return _cmd_attribution(args)
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
