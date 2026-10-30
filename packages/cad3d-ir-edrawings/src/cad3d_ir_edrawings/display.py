"""Pack an edrawings-kit display bundle into one cad3d-ir GLB.

edrawings-kit returns every body once, as packed little-endian buffers in the
model's own units (``float32`` positions and optional normals, ``uint32``
triangle and outline indices), and every placement as a row-vector 4x4 matrix
(``p_world = p_local · M``). This module scales positions to package
millimetres, converts the matrices to cad3d-ir's column-vector convention and
writes one glTF mesh per distinct ``(bodies, colours)`` combination and one
node per placement, so repeated parts share their vertex data. Node ``0``
converts millimetres and Z-up to glTF conventions like every other adapter.
"""

from __future__ import annotations

import struct
import sys
from array import array
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from cad3d_ir.mesh import GlbBuilder
from cad3d_ir.mesh.geometry import Bounds, extend_bounds
from cad3d_ir.model.common import Matrix4

ColorRGBA = tuple[float, float, float, float]

DEFAULT_FACE_COLOR: ColorRGBA = (0.55, 0.62, 0.70, 1.0)
EDGE_COLOR: ColorRGBA = (0.08, 0.10, 0.14, 1.0)
IDENTITY: Matrix4 = (
    (1.0, 0.0, 0.0, 0.0),
    (0.0, 1.0, 0.0, 0.0),
    (0.0, 0.0, 1.0, 0.0),
    (0.0, 0.0, 0.0, 1.0),
)
_BIG_ENDIAN = sys.byteorder == "big"


def instance_matrix(values: Sequence[float], scale: float) -> Matrix4:
    """Convert a kit placement matrix to a cad3d-ir matrix in millimetres.

    The kit stores row-major 4x4 matrices for row vectors in source units
    (rotation in the first three rows, translation in the last row). cad3d-ir
    applies matrices to column vectors in package millimetres, so the 3x3 block
    is transposed and the translation moves to the last column, scaled by
    ``scale`` millimetres per source unit.
    """
    if len(values) != 16:
        raise ValueError("a placement matrix has sixteen values")
    v = [float(value) for value in values]
    if any(value != value for value in v):
        raise ValueError("placement matrix must be finite")
    return (
        (v[0], v[4], v[8], v[12] * scale),
        (v[1], v[5], v[9], v[13] * scale),
        (v[2], v[6], v[10], v[14] * scale),
        (0.0, 0.0, 0.0, 1.0),
    )


def scale_positions(payload: bytes, scale: float) -> bytes:
    """Multiply a packed little-endian ``float32`` buffer by ``scale``."""
    if scale == 1.0:
        return bytes(payload)
    values = array("f")
    values.frombytes(payload)
    if _BIG_ENDIAN:
        values.byteswap()
    scaled = array("f", [value * scale for value in values])
    if _BIG_ENDIAN:
        scaled.byteswap()
    return scaled.tobytes()


def color_key(rgba: Sequence[int]) -> ColorRGBA | None:
    """Normalize a kit RGBA byte quadruple; alpha ``0`` means no saved colour."""
    red, green, blue, alpha = (int(channel) for channel in rgba)
    if alpha == 0:
        return None
    return (red / 255.0, green / 255.0, blue / 255.0, alpha / 255.0)


def color_name(key: ColorRGBA | None) -> str:
    if key is None:
        return "faces"
    name = "#" + "".join(f"{round(channel * 255):02x}" for channel in key[:3])
    if key[3] < 1.0:
        name += f"/{round(key[3] * 255):02x}"
    return name


def transform_point(matrix: Matrix4, point: Sequence[float]) -> tuple[float, ...]:
    x, y, z = point
    return tuple(
        matrix[row][0] * x + matrix[row][1] * y + matrix[row][2] * z + matrix[row][3]
        for row in range(3)
    )


def local_bounds(positions: bytes) -> Bounds | None:
    """Axis-aligned bounds of a packed ``float32`` xyz buffer."""
    count = len(positions) // 12
    if count == 0:
        return None
    flat = struct.unpack(f"<{count * 3}f", positions[: count * 12])
    xs, ys, zs = flat[0::3], flat[1::3], flat[2::3]
    return ((min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs)))


def placed_bounds(bounds: Bounds | None, local: Bounds, matrix: Matrix4) -> Bounds:
    """Extend ``bounds`` by the eight corners of ``local`` moved by ``matrix``."""
    low, high = local
    corners: list[float] = []
    for x in (low[0], high[0]):
        for y in (low[1], high[1]):
            for z in (low[2], high[2]):
                corners.extend(transform_point(matrix, (x, y, z)))
    extended = extend_bounds(bounds, corners)
    assert extended is not None
    return extended


@dataclass(frozen=True, slots=True)
class GlbPlacement:
    """One GLB node: a placement of one or more kit bodies with their colours."""

    name: str
    node_id: str
    product_id: str
    matrix: Matrix4
    bodies: tuple[int, ...]
    colors: tuple[ColorRGBA | None, ...]
    body_ids: tuple[str, ...]


@dataclass(slots=True)
class DisplayGlb:
    """The packed GLB of one display bundle and the counts a manifest needs."""

    payload: bytes
    triangle_count: int
    mesh_triangle_count: int
    line_segment_count: int
    mesh_line_segment_count: int
    vertex_count: int
    node_count: int
    mesh_count: int
    body_count: int
    placement_count: int
    bounds: Bounds | None
    colors: Counter[ColorRGBA]
    uncolored: int

    @property
    def dominant_color(self) -> ColorRGBA | None:
        if not self.colors:
            return None
        return self.colors.most_common(1)[0][0]


def build_display_glb(
    display: Any,
    *,
    name: str,
    scale: float,
    placements: Sequence[GlbPlacement],
    include_edges: bool,
    generator: str,
    root_extras: dict[str, Any] | None = None,
) -> DisplayGlb:
    """Write the placements of one display bundle as a GLB with shared meshes.

    Each kit body is written once as a vertex set (positions scaled to
    millimetres, stored normals kept). A glTF mesh exists per distinct
    ``(bodies, colours, body ids)`` tuple with one TRIANGLES primitive per body
    (``extras.ranges`` = ``[[pick_id, 0, index_count]]`` where ``pick_id`` is
    the 1-based kit body index) and, when requested, one LINES primitive per
    body for the stored outline edges. A node per placement carries the
    occurrence id in ``extras.id`` and the converted matrix.
    """
    summary = display.summary
    builder = GlbBuilder(
        generator=generator,
        root_name=name,
        length_unit_to_m=0.001,
        up_axis="z",
        root_extras=root_extras,
    )
    vertex_sets: dict[int, tuple[int, Bounds | None]] = {}
    triangles: dict[int, bytes] = {}
    edges: dict[int, bytes] = {}
    materials: dict[ColorRGBA | None, int] = {}
    edge_material: int | None = None
    meshes: dict[tuple[Any, ...], int] = {}
    colors: Counter[ColorRGBA] = Counter()
    uncolored = 0
    bounds: Bounds | None = None
    placed_triangles = 0
    placed_segments = 0
    placement_count = 0

    def vertex_set(body: int) -> tuple[int, Bounds | None]:
        cached = vertex_sets.get(body)
        if cached is None:
            buffers = display.body_buffers(body)
            positions = scale_positions(buffers.positions.data, scale)
            normals = None if buffers.normals is None else bytes(buffers.normals.data)
            if normals is not None and len(normals) != len(positions):
                normals = None
            handle = builder.add_vertices(positions=positions, normals=normals)
            cached = (handle, local_bounds(positions))
            vertex_sets[body] = cached
            triangles[body] = bytes(buffers.triangles.data)
            edges[body] = bytes(buffers.edges.data)
        return cached

    def material_for(key: ColorRGBA | None) -> int:
        material = materials.get(key)
        if material is None:
            material = builder.add_material(
                color_name(key), DEFAULT_FACE_COLOR if key is None else key
            )
            materials[key] = material
        return material

    for placement in placements:
        mesh_key = (placement.bodies, placement.colors, placement.body_ids)
        mesh = meshes.get(mesh_key)
        if mesh is None:
            mesh = builder.add_mesh(
                placement.name,
                extras={
                    "product": placement.product_id,
                    "bodies": list(placement.body_ids),
                },
            )
            meshes[mesh_key] = mesh
            for body, key, body_id in zip(
                placement.bodies, placement.colors, placement.body_ids, strict=True
            ):
                info = summary.bodies[body]
                if info.triangle_count <= 0:
                    continue
                handle, _ = vertex_set(body)
                if not triangles[body]:
                    continue
                builder.add_triangles(
                    mesh,
                    vertices=handle,
                    indices=triangles[body],
                    material=material_for(key),
                    extras={
                        "entityKind": "face",
                        "body": body_id,
                        "ranges": [[body + 1, 0, info.triangle_count * 3]],
                    },
                )
                if include_edges and info.edge_count > 0 and edges[body]:
                    if edge_material is None:
                        edge_material = builder.add_material(
                            "edges", EDGE_COLOR, unlit=True
                        )
                    builder.add_lines(
                        mesh,
                        vertices=handle,
                        indices=edges[body],
                        material=edge_material,
                        extras={
                            "entityKind": "edge",
                            "body": body_id,
                            "ranges": [[body + 1, 0, info.edge_count * 2]],
                        },
                    )
        for body, key in zip(placement.bodies, placement.colors, strict=True):
            info = summary.bodies[body]
            if key is None:
                uncolored += 1
            else:
                colors[key] += 1
            placed_triangles += info.triangle_count
            if include_edges:
                placed_segments += info.edge_count
            cached = vertex_sets.get(body)
            if cached is not None and cached[1] is not None:
                bounds = placed_bounds(bounds, cached[1], placement.matrix)
        builder.add_node(
            placement.name,
            mesh=mesh,
            matrix=None if placement.matrix == IDENTITY else placement.matrix,
            extras={"id": placement.node_id, "product": placement.product_id},
        )
        placement_count += 1

    payload = builder.build()
    return DisplayGlb(
        payload=payload,
        triangle_count=placed_triangles,
        mesh_triangle_count=builder.triangle_count,
        line_segment_count=placed_segments,
        mesh_line_segment_count=builder.line_segment_count,
        vertex_count=builder.vertex_count,
        node_count=builder.node_count,
        mesh_count=builder.mesh_count,
        body_count=len(vertex_sets),
        placement_count=placement_count,
        bounds=bounds,
        colors=colors,
        uncolored=uncolored,
    )


__all__ = [
    "DEFAULT_FACE_COLOR",
    "EDGE_COLOR",
    "IDENTITY",
    "DisplayGlb",
    "GlbPlacement",
    "build_display_glb",
    "color_key",
    "color_name",
    "instance_matrix",
    "local_bounds",
    "placed_bounds",
    "scale_positions",
    "transform_point",
]
