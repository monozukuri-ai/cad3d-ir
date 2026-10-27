"""OCCT helpers: part solids, bounds, tessellation into one glTF binary, B-Rep bytes.

Everything here works on the part-frame compounds that icadkit builds
(:func:`icadkit.read_part_shapes` with ``frame="part"``) and on the rigid
part frames of :class:`icadkit.PartPlacement`; it does not read ICD bytes.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from cad3d_ir.mesh import GlbBuilder
from cad3d_ir_step.tessellate import ShapeMesh, mesh_shape

Vector3 = tuple[float, float, float]
Bounds = tuple[Vector3, Vector3]
Matrix4 = tuple[tuple[float, ...], ...]

IDENTITY: Matrix4 = (
    (1.0, 0.0, 0.0, 0.0),
    (0.0, 1.0, 0.0, 0.0),
    (0.0, 0.0, 1.0, 0.0),
    (0.0, 0.0, 0.0, 1.0),
)
FACE_COLOR = (0.72, 0.74, 0.78, 1.0)
EDGE_COLOR = (0.10, 0.10, 0.12, 1.0)


@dataclass(slots=True)
class PartSolid:
    """One part's converted solids in the part frame, plus the sharing key."""

    part_id: str
    name: str
    shape: Any
    key: str
    sources: tuple[str, ...]
    bounds: Bounds | None
    world_transform: Matrix4


def shape_bounds(shape: Any) -> Bounds | None:
    """Conservative bounds from the control points (never too small)."""
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box, False)
    if box.IsVoid():
        return None
    xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
    return (
        (float(xmin), float(ymin), float(zmin)),
        (float(xmax), float(ymax), float(zmax)),
    )


def transform_point(matrix: Matrix4, point: Vector3) -> Vector3:
    x, y, z = point
    return tuple(  # type: ignore[return-value]
        float(row[0] * x + row[1] * y + row[2] * z + row[3]) for row in matrix[:3]
    )


def transform_bounds(bounds: Bounds, matrix: Matrix4) -> Bounds:
    """Bounds of the eight corners after a rigid transform."""
    (x0, y0, z0), (x1, y1, z1) = bounds
    corners = [
        transform_point(matrix, (x, y, z))
        for x in (x0, x1)
        for y in (y0, y1)
        for z in (z0, z1)
    ]
    return (
        (
            min(c[0] for c in corners),
            min(c[1] for c in corners),
            min(c[2] for c in corners),
        ),
        (
            max(c[0] for c in corners),
            max(c[1] for c in corners),
            max(c[2] for c in corners),
        ),
    )


def merge_bounds(first: Bounds | None, second: Bounds | None) -> Bounds | None:
    if first is None:
        return second
    if second is None:
        return first
    return (
        tuple(min(a, b) for a, b in zip(first[0], second[0], strict=True)),  # type: ignore[return-value]
        tuple(max(a, b) for a, b in zip(first[1], second[1], strict=True)),  # type: ignore[return-value]
    )


def matmul(left: Matrix4, right: Matrix4) -> Matrix4:
    return tuple(
        tuple(
            sum(float(left[i][k]) * float(right[k][j]) for k in range(4))
            for j in range(4)
        )
        for i in range(4)
    )


def diagonal(bounds: Bounds | None) -> float:
    if bounds is None:
        return 0.0
    return sum((b - a) ** 2 for a, b in zip(bounds[0], bounds[1], strict=True)) ** 0.5


def sharing_key(bodies: Iterable[Any], bounds: Bounds | None, *, mirrored: bool) -> str:
    """Identify identical part-frame solids by payloads, sources, bounds and parity.

    Two parts whose saved bodies have the same payload hashes, the same source
    kinds and the same part-frame bounds (to a micrometre) show the same solid
    in their own frames, so they can share one mesh. Bodies without a payload
    hash (native primitives, CSG results) never share.
    """
    items = []
    for body in bodies:
        digest = getattr(body, "payload_sha256", None)
        items.append(f"{body.source}:{digest or f'unique:{body.body_id}'}")
    text = "|".join(sorted(items))
    if bounds is not None:
        text += "|" + ",".join(f"{v:.3f}" for v in (*bounds[0], *bounds[1]))
    text += "|mirror" if mirrored else "|proper"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def brep_bytes(shape: Any) -> bytes:
    """Serialize a shape as OCCT's ASCII BREP (version 1 header)."""
    from OCP.BRepTools import BRepTools

    handle, path = tempfile.mkstemp(prefix="cad3d-ir-icad-", suffix=".brep")
    os.close(handle)
    try:
        if not BRepTools.Write_s(shape, path):
            raise RuntimeError("OCCT could not serialize the shape as BREP")
        with open(path, "rb") as stream:
            return stream.read()
    finally:
        os.unlink(path)


def location_from_matrix(matrix: Matrix4) -> Any:
    """A ``TopLoc_Location`` for a rigid row-major 4x4 matrix (mm)."""
    from OCP.gp import gp_Trsf
    from OCP.TopLoc import TopLoc_Location

    trsf = gp_Trsf()
    (a11, a12, a13, a14), (a21, a22, a23, a24), (a31, a32, a33, a34) = (
        tuple(float(v) for v in row) for row in matrix[:3]
    )
    trsf.SetValues(a11, a12, a13, a14, a21, a22, a23, a24, a31, a32, a33, a34)
    return TopLoc_Location(trsf)


@dataclass(slots=True)
class GlbNode:
    """One glTF node request: a part of the tree, with or without a mesh."""

    name: str
    parent: int | None
    matrix: Matrix4
    key: str | None
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class GlbResult:
    """The built GLB; ``*_count`` describe mesh primitives, ``placed_*`` the nodes.

    ``bounds`` are the mesh-local bounds the builder tracks; ``placed_bounds``
    apply every node's accumulated transform to its mesh bounds.
    """

    payload: bytes
    triangle_count: int
    line_segment_count: int
    vertex_count: int
    node_count: int
    mesh_count: int
    bounds: Bounds | None
    placed_bounds: Bounds | None
    placed_triangles: int
    placed_line_segments: int
    mesh_triangles: dict[str, int]
    failed_keys: dict[str, str]


def _add_shape_mesh(
    builder: GlbBuilder,
    mesh: int,
    shape_mesh: ShapeMesh,
    *,
    face_material: int,
    edge_material: int | None,
) -> None:
    """Append one definition's faces (one primitive) and edges (one primitive)."""
    if shape_mesh.faces:
        positions: list[float] = []
        normals: list[float] = []
        indices: list[int] = []
        ranges: list[list[int]] = []
        for face in shape_mesh.faces:
            offset = len(positions) // 3
            start = len(indices)
            positions.extend(face.positions)
            normals.extend(face.normals)
            indices.extend(index + offset for index in face.indices)
            ranges.append([face.pick_id, start, len(face.indices)])
        builder.add_triangles(
            mesh,
            positions=positions,
            normals=normals,
            indices=indices,
            material=face_material,
            extras={"entityKind": "face", "ranges": ranges},
        )
    if shape_mesh.edges and edge_material is not None:
        positions = []
        indices = []
        ranges = []
        for edge in shape_mesh.edges:
            offset = len(positions) // 3
            start = len(indices)
            positions.extend(edge.positions)
            point_count = len(edge.positions) // 3
            for index in range(point_count - 1):
                indices.extend((offset + index, offset + index + 1))
            ranges.append([edge.pick_id, start, len(indices) - start])
        builder.add_lines(
            mesh,
            positions=positions,
            indices=indices,
            material=edge_material,
            extras={"entityKind": "edge", "ranges": ranges},
        )


def build_glb(
    nodes: Sequence[GlbNode],
    shapes: dict[str, Any],
    *,
    linear_deflection: float,
    angular_deflection: float,
    include_edges: bool,
    generator: str,
    root_name: str,
    root_extras: dict[str, Any] | None = None,
) -> GlbResult:
    """Tessellate each distinct shape once and place the nodes of the tree.

    ``nodes`` are in depth-first order with ``parent`` indices into the same
    sequence; ``shapes`` maps a sharing key to its part-frame compound.
    """
    builder = GlbBuilder(
        generator=generator,
        root_name=root_name,
        length_unit_to_m=0.001,
        up_axis="z",
        root_extras=root_extras,
    )
    face_material = builder.add_material("faces", FACE_COLOR)
    edge_material = (
        builder.add_material("edges", EDGE_COLOR, unlit=True) if include_edges else None
    )
    mesh_by_key: dict[str, int] = {}
    mesh_triangles: dict[str, int] = {}
    mesh_segments: dict[str, int] = {}
    mesh_bounds: dict[str, Bounds] = {}
    failed: dict[str, str] = {}
    for key, shape in shapes.items():
        try:
            shape_mesh = mesh_shape(
                shape,
                linear_deflection=linear_deflection,
                angular_deflection=angular_deflection,
                include_edges=include_edges,
            )
        except Exception as exc:  # noqa: BLE001 - OCCT failures are reported per shape
            failed[key] = f"{type(exc).__name__}: {exc}"
            continue
        if not shape_mesh.faces and not shape_mesh.edges:
            failed[key] = "empty tessellation"
            continue
        mesh = builder.add_mesh(key)
        _add_shape_mesh(
            builder,
            mesh,
            shape_mesh,
            face_material=face_material,
            edge_material=edge_material,
        )
        mesh_by_key[key] = mesh
        mesh_triangles[key] = shape_mesh.triangle_count
        mesh_segments[key] = shape_mesh.line_segment_count
        if shape_mesh.bounds is not None:
            mesh_bounds[key] = (
                tuple(shape_mesh.bounds[0]),  # type: ignore[assignment]
                tuple(shape_mesh.bounds[1]),
            )
    indices: list[int] = []
    worlds: list[Matrix4] = []
    placed_bounds: Bounds | None = None
    placed_triangles = 0
    placed_segments = 0
    for node in nodes:
        parent = None if node.parent is None else indices[node.parent]
        parent_world = IDENTITY if node.parent is None else worlds[node.parent]
        world = matmul(parent_world, node.matrix)
        worlds.append(world)
        mesh = mesh_by_key.get(node.key) if node.key is not None else None
        if node.key is not None and mesh is not None:
            placed_triangles += mesh_triangles[node.key]
            placed_segments += mesh_segments[node.key]
            if node.key in mesh_bounds:
                placed_bounds = merge_bounds(
                    placed_bounds, transform_bounds(mesh_bounds[node.key], world)
                )
        indices.append(
            builder.add_node(
                node.name,
                mesh=mesh,
                parent=parent,
                matrix=node.matrix,
                extras=node.extras or None,
            )
        )
    bounds = builder.bounds
    return GlbResult(
        payload=builder.build(),
        triangle_count=builder.triangle_count,
        line_segment_count=builder.line_segment_count,
        vertex_count=builder.vertex_count,
        node_count=builder.node_count,
        mesh_count=builder.mesh_count,
        bounds=(tuple(bounds[0]), tuple(bounds[1])) if bounds is not None else None,  # type: ignore[arg-type]
        placed_bounds=placed_bounds,
        placed_triangles=placed_triangles,
        placed_line_segments=placed_segments,
        mesh_triangles=mesh_triangles,
        failed_keys=failed,
    )


__all__ = [
    "EDGE_COLOR",
    "FACE_COLOR",
    "IDENTITY",
    "Bounds",
    "GlbNode",
    "GlbResult",
    "Matrix4",
    "PartSolid",
    "brep_bytes",
    "build_glb",
    "diagonal",
    "location_from_matrix",
    "matmul",
    "merge_bounds",
    "shape_bounds",
    "sharing_key",
    "transform_bounds",
    "transform_point",
]
