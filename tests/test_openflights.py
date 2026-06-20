"""Loading a synthetic flight Timetable from OpenFlights-format data (no network)."""

from datetime import datetime, timedelta

from travelplanner.models import Mode, CostLevel
from travelplanner.openflights import load_openflights
from travelplanner.graph.schema import NodeType
from travelplanner.graph.scheduled import ConnectionScan

# OpenFlights airports.dat rows (no header): id,name,city,country,IATA,ICAO,
# lat,lon,alt,tz,dst,tzdb,type,source. AMS, ZRH, plus one without an IATA code.
AIRPORTS = '''\
580,"Amsterdam Schiphol","Amsterdam","Netherlands","AMS","EHAM",52.3105,4.7683,-11,1,"E","Europe/Amsterdam","airport","OurAirports"
1678,"Zurich","Zurich","Switzerland","ZRH","LSZH",47.4647,8.5492,1416,1,"E","Europe/Zurich","airport","OurAirports"
9999,"No IATA Field","Nowhere","Nowhere","\\N","XXXX",10.0,10.0,0,0,"U","\\N","airport","OurAirports"
'''

# routes.dat rows (no header): airline,airlineID,src,srcID,dst,dstID,codeshare,
# stops,equipment. A non-stop AMS-ZRH (twice, codeshare), one 1-stop (dropped),
# and one referencing the IATA-less airport (dropped).
ROUTES = '''\
KL,3090,AMS,580,ZRH,1678,,0,738
LX,4559,AMS,580,ZRH,1678,Y,0,32A
KL,3090,ZRH,1678,AMS,580,,0,738
XX,1,AMS,580,ZRH,1678,,1,738
YY,2,AMS,580,XXX,9999,,0,738
'''


def _feed(tmp_path):
    a = tmp_path / "airports.dat"
    r = tmp_path / "routes.dat"
    a.write_text(AIRPORTS, encoding="utf-8")
    r.write_text(ROUTES, encoding="utf-8")
    return str(a), str(r)


def test_loads_airports_and_directed_routes(tmp_path):
    a, r = _feed(tmp_path)
    tt = load_openflights(a, r, depart_hours=(8,))
    # only airports with an IATA code AND a flight are kept
    assert set(tt.stops) == {"AMS", "ZRH"}
    assert all(s.type is NodeType.AIRPORT for s in tt.stops.values())
    # two directed non-stop pairs (AMS->ZRH dedups the codeshare; ZRH->AMS),
    # one trip each at the single departure hour
    assert len(tt.trips) == 2
    assert all(t.mode is Mode.FLIGHT and t.cost_level is CostLevel.HIGH
               for t in tt.trips.values())


def test_one_stop_and_unknown_airport_routes_dropped(tmp_path):
    a, r = _feed(tmp_path)
    tt = load_openflights(a, r, depart_hours=(8,))
    # the 1-stop AMS-ZRH and the AMS-XXX (unknown airport) rows produce no trips
    pairs = {(t.stop_times[0].stop_id, t.stop_times[1].stop_id)
             for t in tt.trips.values()}
    assert pairs == {("AMS", "ZRH"), ("ZRH", "AMS")}


def test_multiple_departures_and_synthetic_duration(tmp_path):
    a, r = _feed(tmp_path)
    tt = load_openflights(a, r, depart_hours=(6, 18), cruise_kmh=800.0,
                          overhead=timedelta(minutes=45))
    # 2 directed pairs x 2 departures = 4 trips
    assert len(tt.trips) == 4
    # AMS-ZRH great-circle ~600 km -> ~45 min over + ~45 min cruise ~= 1h30
    amsz = next(t for t in tt.trips.values()
                if t.stop_times[0].stop_id == "AMS" and t.id.endswith("@06"))
    dur = amsz.stop_times[1].arrival - amsz.stop_times[0].departure
    assert timedelta(minutes=60) < dur < timedelta(minutes=120)


def test_keep_filters_airports(tmp_path):
    a, r = _feed(tmp_path)
    tt = load_openflights(a, r, keep={"AMS"}, depart_hours=(8,))
    # with only AMS kept, no route has both endpoints -> empty network
    assert tt.stops == {} and tt.trips == {}


def test_routes_into_a_real_journey(tmp_path):
    """The synthesized flights actually route end to end via CSA."""
    a, r = _feed(tmp_path)
    tt = load_openflights(a, r, depart_hours=(8,))
    j = ConnectionScan(tt).query({"AMS": datetime(2026, 7, 1, 6, 0)}, "ZRH")
    assert j is not None
    assert j.legs[0].mode is Mode.FLIGHT
    assert j.legs[0].from_stop == "AMS" and j.legs[-1].to_stop == "ZRH"


def test_requires_paths_or_download(tmp_path):
    import pytest
    with pytest.raises(ValueError):
        load_openflights()                       # no paths, no download
