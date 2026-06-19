"""NodeGrid nearest-node index: must agree with a brute-force scan."""

from array import array

from travelplanner.geo import haversine
from travelplanner.graph.road.spatial import NodeGrid


def _brute(lat, lon, lats, lons):
    best_i, best_d = -1, float("inf")
    for i in range(len(lats)):
        d = haversine(lat, lon, lats[i], lons[i])
        if d < best_d:
            best_i, best_d = i, d
    return best_i, best_d


# A spread of nodes across a country-sized box (roughly Switzerland).
LATS = array("d", [46.69, 46.70, 46.56, 46.32, 46.69, 46.50, 46.29, 46.95,
                   47.38, 47.05, 46.01, 46.20, 47.50, 46.84, 47.22])
LONS = array("d", [7.86, 8.24, 8.36, 7.99, 7.68, 7.67, 7.88, 7.44,
                   8.54, 7.00, 8.96, 6.14, 8.74, 9.53, 8.82])

QUERIES = [
    (46.69, 7.86),    # exactly on a node
    (46.948, 7.447),  # next to bern
    (46.40, 8.10),    # interior gap
    (45.00, 6.00),    # south-west, outside the cloud
    (48.50, 10.0),    # north-east, outside the cloud
    (46.5, 7.95),
    (47.10, 8.30),
]


def test_grid_matches_bruteforce():
    grid = NodeGrid.build(LATS, LONS)
    for lat, lon in QUERIES:
        gi, gd = grid.nearest(lat, lon)
        bi, bd = _brute(lat, lon, LATS, LONS)
        # The index (or a coincident node) and the distance must match.
        assert abs(gd - bd) < 1e-6, (lat, lon, gd, bd)
        assert abs(LATS[gi] - LATS[bi]) < 1e-9
        assert abs(LONS[gi] - LONS[bi]) < 1e-9


def test_empty_grid():
    grid = NodeGrid.build(array("d"), array("d"))
    assert grid.nearest(46.0, 8.0) == (-1, float("inf"))


def test_single_node():
    grid = NodeGrid.build(array("d", [46.0]), array("d", [8.0]))
    i, d = grid.nearest(46.5, 8.5)
    assert i == 0
    assert abs(d - haversine(46.5, 8.5, 46.0, 8.0)) < 1e-6


def test_varied_cell_sizes():
    # Result must be independent of cell granularity.
    coarse = NodeGrid.build(LATS, LONS, cell_km=25.0)
    fine = NodeGrid.build(LATS, LONS, cell_km=0.25)
    for lat, lon in QUERIES:
        _, dc = coarse.nearest(lat, lon)
        _, df = fine.nearest(lat, lon)
        _, db = _brute(lat, lon, LATS, LONS)
        assert abs(dc - db) < 1e-6
        assert abs(df - db) < 1e-6
