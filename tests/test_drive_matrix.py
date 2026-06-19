"""drive_matrix over a small offline artifact (no network/OSM download)."""

import pytest

from travelplanner.graph.road.model import RoadGraphBuilder

routingkit = pytest.importorskip("routingkit_cch")

from travelplanner.graph.road import CCHRoadRouter  # noqa: E402
from travelplanner.graph.road.store import save_road_artifact  # noqa: E402
from travelplanner import drive, drive_matrix, road_router  # noqa: E402


def _artifact(tmp_path):
    b = RoadGraphBuilder(store_names=False)
    coords = {1: (47.10, 9.50), 2: (47.12, 9.52), 3: (47.15, 9.55)}
    for k, (lat, lon) in coords.items():
        b.add_node(k, lat, lon)
    b.add_road(1, 2, 120)
    b.add_road(2, 3, 180)
    g = b.build()
    save_road_artifact(g, CCHRoadRouter(g).order, str(tmp_path))
    return str(tmp_path), coords


def test_drive_matrix_shape_and_diagonal(tmp_path):
    road_router.cache_clear()
    data_dir, coords = _artifact(tmp_path)
    pts = [coords[1], coords[2], coords[3]]
    m = drive_matrix(pts, "tiny", data_dir=data_dir)

    assert len(m) == 3 and all(len(row) == 3 for row in m)
    for i in range(3):
        assert m[i][i].drivable
        assert m[i][i].distance_km == 0.0
    # off-diagonal pairs are connected with positive duration
    assert m[0][2].drivable
    assert m[0][2].duration.total_seconds() > 0


def test_drive_matrix_matches_drive(tmp_path):
    road_router.cache_clear()
    data_dir, coords = _artifact(tmp_path)
    pts = [coords[1], coords[3]]
    m = drive_matrix(pts, "tiny", data_dir=data_dir)
    single = drive(coords[1], coords[3], region="tiny", data_dir=data_dir)
    assert m[0][1].drivable == single.drivable
    assert m[0][1].duration == single.duration
    assert m[0][1].distance_km == single.distance_km


def test_drive_matrix_rectangular(tmp_path):
    road_router.cache_clear()
    data_dir, coords = _artifact(tmp_path)
    m = drive_matrix([coords[1]], "tiny", dests=[coords[2], coords[3]],
                     data_dir=data_dir)
    assert len(m) == 1 and len(m[0]) == 2
    assert m[0][0].drivable and m[0][1].drivable
