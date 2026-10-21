"""Small, allocation-conscious triangle helpers shared by importers."""

from __future__ import annotations

from math import sqrt

Bounds = tuple[tuple[float, float, float], tuple[float, float, float]]


def vertex_normals(positions: list[float], indices: list[int]) -> list[float]:
    """Return area-weighted, normalized per-vertex normals for an indexed mesh.

    Vertices that no triangle references, and vertices whose incident
    triangles cancel out, receive the +Z unit normal so the array always holds
    finite unit vectors.
    """
    normals = [0.0] * len(positions)
    for index in range(0, len(indices), 3):
        first, second, third = indices[index : index + 3]
        ax, ay, az = positions[first * 3 : first * 3 + 3]
        bx, by, bz = positions[second * 3 : second * 3 + 3]
        cx, cy, cz = positions[third * 3 : third * 3 + 3]
        ux, uy, uz = bx - ax, by - ay, bz - az
        vx, vy, vz = cx - ax, cy - ay, cz - az
        nx = uy * vz - uz * vy
        ny = uz * vx - ux * vz
        nz = ux * vy - uy * vx
        for vertex in (first, second, third):
            normals[vertex * 3] += nx
            normals[vertex * 3 + 1] += ny
            normals[vertex * 3 + 2] += nz
    for index in range(0, len(normals), 3):
        nx, ny, nz = normals[index : index + 3]
        length = sqrt(nx * nx + ny * ny + nz * nz)
        if length == 0.0:
            normals[index : index + 3] = [0.0, 0.0, 1.0]
        else:
            normals[index : index + 3] = [nx / length, ny / length, nz / length]
    return normals


def flat_normals(positions: list[float]) -> list[float]:
    """Return one face normal per vertex of a de-indexed triangle soup.

    ``positions`` holds nine floats per triangle. Degenerate triangles receive
    the +Z unit normal; remove them first with :func:`drop_degenerate_triangles`
    when they should not be rendered at all.
    """
    if len(positions) % 9:
        raise ValueError("a triangle soup needs nine coordinates per triangle")
    normals: list[float] = []
    for index in range(0, len(positions), 9):
        ax, ay, az, bx, by, bz, cx, cy, cz = positions[index : index + 9]
        ux, uy, uz = bx - ax, by - ay, bz - az
        vx, vy, vz = cx - ax, cy - ay, cz - az
        nx = uy * vz - uz * vy
        ny = uz * vx - ux * vz
        nz = ux * vy - uy * vx
        length = sqrt(nx * nx + ny * ny + nz * nz)
        if length == 0.0:
            normal = (0.0, 0.0, 1.0)
        else:
            normal = (nx / length, ny / length, nz / length)
        normals.extend(normal * 3)
    return normals


def drop_degenerate_triangles(positions: list[float]) -> tuple[list[float], int]:
    """Remove zero-area triangles from a de-indexed soup; return it and the count."""
    if len(positions) % 9:
        raise ValueError("a triangle soup needs nine coordinates per triangle")
    kept: list[float] = []
    dropped = 0
    for index in range(0, len(positions), 9):
        triangle = positions[index : index + 9]
        ax, ay, az, bx, by, bz, cx, cy, cz = triangle
        ux, uy, uz = bx - ax, by - ay, bz - az
        vx, vy, vz = cx - ax, cy - ay, cz - az
        nx = uy * vz - uz * vy
        ny = uz * vx - ux * vz
        nz = ux * vy - uy * vx
        if nx == 0.0 and ny == 0.0 and nz == 0.0:
            dropped += 1
            continue
        kept.extend(triangle)
    return kept, dropped


def extend_bounds(bounds: Bounds | None, positions: list[float]) -> Bounds | None:
    """Grow an axis-aligned bounding box with a flat ``xyzxyz...`` array."""
    if not positions:
        return bounds
    xs = positions[0::3]
    ys = positions[1::3]
    zs = positions[2::3]
    low = (min(xs), min(ys), min(zs))
    high = (max(xs), max(ys), max(zs))
    if bounds is None:
        return low, high
    return (
        (
            min(bounds[0][0], low[0]),
            min(bounds[0][1], low[1]),
            min(bounds[0][2], low[2]),
        ),
        (
            max(bounds[1][0], high[0]),
            max(bounds[1][1], high[1]),
            max(bounds[1][2], high[2]),
        ),
    )
