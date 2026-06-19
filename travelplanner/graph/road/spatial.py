"""Uniform-grid spatial index for fast nearest-road-node snapping.

A country-scale road graph has millions of nodes; finding the node nearest a
query coordinate by scanning them all is O(n) per snap. NodeGrid buckets nodes
into a uniform lat/lon grid whose cells are roughly square in kilometres, then
answers nearest-node by scanning the home cell and expanding square rings of
cells until no unexamined cell could hold anything closer than the best found.

The index depends only on the coordinate arrays, so it is cheap to rebuild and
straightforward to persist alongside the road graph.
"""

import math
from array import array

from travelplanner.geo import haversine

KM_PER_DEG_LAT = 111.32
# Cells are sized in kilometres so the ring-search stopping bound is simply
# (rings * cell_km).
DEFAULT_CELL_KM = 1.0


class NodeGrid:
    """Nearest-node lookup over a fixed set of (latitude, longitude) coordinates."""

    def __init__(self, latitude: array, longitude: array, *,
                 cell_lat_deg: float, cell_lon_deg: float, cell_km: float,
                 cells: dict, min_cell: tuple[int, int],
                 max_cell: tuple[int, int]) -> None:
        self.latitude = latitude
        self.longitude = longitude
        self.cell_lat_deg = cell_lat_deg
        self.cell_lon_deg = cell_lon_deg
        self.cell_km = cell_km
        self.cells = cells              # (i, j) -> array("i") of node indices
        self.min_cell = min_cell
        self.max_cell = max_cell

    @classmethod
    def build(cls, latitude: array, longitude: array, *,
              cell_km: float = DEFAULT_CELL_KM) -> "NodeGrid":
        n = len(latitude)
        # A degree of longitude shrinks toward the poles. Sizing the longitude
        # cell at the extreme latitude (smallest cosine) guarantees every cell
        # is at least cell_km wide in km, so the ring-search bound r * cell_km
        # is a true lower bound on the distance to any unscanned node.
        max_abs_lat = max((abs(latitude[i]) for i in range(n)), default=0.0)
        km_per_deg_lon = max(1e-6, KM_PER_DEG_LAT * math.cos(math.radians(max_abs_lat)))
        cell_lat_deg = cell_km / KM_PER_DEG_LAT
        cell_lon_deg = cell_km / km_per_deg_lon

        cells: dict[tuple[int, int], array] = {}
        for idx in range(n):
            i = math.floor(latitude[idx] / cell_lat_deg)
            j = math.floor(longitude[idx] / cell_lon_deg)
            bucket = cells.get((i, j))
            if bucket is None:
                cells[(i, j)] = bucket = array("i")
            bucket.append(idx)
        keys = cells.keys()
        min_i = min((k[0] for k in keys), default=0)
        max_i = max((k[0] for k in keys), default=0)
        min_j = min((k[1] for k in keys), default=0)
        max_j = max((k[1] for k in keys), default=0)
        return cls(latitude, longitude, cell_lat_deg=cell_lat_deg,
                   cell_lon_deg=cell_lon_deg, cell_km=cell_km, cells=cells,
                   min_cell=(min_i, min_j), max_cell=(max_i, max_j))

    def _ring_cells(self, ci: int, cj: int, r: int):
        if r == 0:
            yield (ci, cj)
            return
        for dj in range(-r, r + 1):
            yield (ci - r, cj + dj)
            yield (ci + r, cj + dj)
        for di in range(-r + 1, r):
            yield (ci + di, cj - r)
            yield (ci + di, cj + r)

    def nearest(self, lat: float, lon: float) -> tuple[int, float]:
        """Return (node_index, distance_km) of the nearest node.

        Returns (-1, inf) only if the grid is empty.
        """
        if not self.cells:
            return -1, float("inf")
        ci = math.floor(lat / self.cell_lat_deg)
        cj = math.floor(lon / self.cell_lon_deg)
        # Largest ring radius that can still reach a populated cell.
        min_i, min_j = self.min_cell
        max_i, max_j = self.max_cell
        max_r = max(ci - min_i, max_i - ci, cj - min_j, max_j - cj, 0)

        best_i, best_d = -1, float("inf")
        r = 0
        while True:
            for cell in self._ring_cells(ci, cj, r):
                bucket = self.cells.get(cell)
                if bucket is None:
                    continue
                for node in bucket:
                    d = haversine(lat, lon, self.latitude[node],
                                  self.longitude[node])
                    if d < best_d:
                        best_i, best_d = node, d
            # Every node not yet scanned sits in a cell at ring >= r + 1, hence
            # at least r * cell_km away (cells are >= cell_km wide in km), so once
            # the best found is within that bound nothing closer can remain.
            if best_i >= 0 and best_d <= r * self.cell_km:
                break
            if r > max_r:
                break
            r += 1
        return best_i, best_d
