from travelplanner.geo import haversine, road_distance


def test_haversine_known_pair():
    # London to Paris is ~344 km great-circle.
    d = haversine(51.5074, -0.1278, 48.8566, 2.3522)
    assert 330 < d < 360


def test_haversine_zero():
    assert haversine(40.0, -74.0, 40.0, -74.0) == 0.0


def test_road_distance_applies_detour():
    air = haversine(51.5074, -0.1278, 48.8566, 2.3522)
    road = road_distance(51.5074, -0.1278, 48.8566, 2.3522, detour_factor=1.3)
    assert abs(road - air * 1.3) < 1e-6
