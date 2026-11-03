"""OCCT helpers: bounds, face lookup, tessellation into one glTF binary, B-Rep bytes.

Everything here works on the body shapes that parasolid-kit's ``to_occt``
returns (millimetres, one solid, shell or compound per Parasolid body) and on
row-major rigid matrices; it does not read X_T / X_B bytes.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from cad3d_ir.mesh import GlbBuilder
from cad3d_ir_step.tessellate import FaceMesh, ShapeMesh, mesh_shape

Vector3 = tuple[float, float, float]
Bounds = tuple[Vector3, Vector3]
Matrix4 = tuple[tuple[float, ...], ...]
Rgba = tuple[float, float, float, float]

IDENTITY: Matrix4 = (
    (1.0, 0.0, 0.0, 0.0),
    (0.0, 1.0, 0.0, 0.0),
    (0.0, 0.0, 1.0, 0.0),
    (0.0, 0.0, 0.0, 1.0),
)
FACE_COLOR: Rgba = (0.72, 0.74, 0.78, 1.0)
EDGE_COLOR: Rgba = (0.10, 0.10, 0.12, 1.0)


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
    """Bounds of the eight corners after an affine transform."""
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


def brep_bytes(shape: Any) -> bytes:
    """Serialize a shape as OCCT's ASCII BREP (version 1 header)."""
    from OCP.BRepTools import BRepTools

    handle, path = tempfile.mkstemp(prefix="cad3d-ir-parasolid-", suffix=".brep")
    os.close(handle)
    try:
        if not BRepTools.Write_s(shape, path):
            raise RuntimeError("OCCT could not serialize the shape as BREP")
        with open(path, "rb") as stream:
            return stream.read()
    finally:
        os.unlink(path)


def location_from_matrix(matrix: Matrix4) -> Any:
    """A ``TopLoc_Location`` for a row-major 4x4 matrix (mm)."""
    from OCP.gp import gp_Trsf
    from OCP.TopLoc import TopLoc_Location

    trsf = gp_Trsf()
    (a11, a12, a13, a14), (a21, a22, a23, a24), (a31, a32, a33, a34) = (
        tuple(float(v) for v in row) for row in matrix[:3]
    )
    trsf.SetValues(a11, a12, a13, a14, a21, a22, a23, a24, a31, a32, a33, a34)
    return TopLoc_Location(trsf)


def face_pick_ids(shape: Any, faces: Iterable[Any]) -> list[int]:
    """The 1-based ``TopExp::MapShapes`` index of each face in ``shape``.

    :func:`cad3d_ir_step.tessellate.mesh_shape` numbers its face meshes by the
    same map, so the indices pair the conversion's face subshapes with their
    tessellation. A face that is not part of the shape yields ``0``.
    """
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedMapOfShape

    face_map = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_FACE, face_map)
    return [int(face_map.FindIndex(face)) for face in faces]


@dataclass(slots=True)
class GlbNode:
    """One glTF node request: a placement of the tree, with or without a mesh."""

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
    material_count: int
    bounds: Bounds | None
    placed_bounds: Bounds | None
    placed_triangles: int
    placed_line_segments: int
    mesh_triangles: dict[str, int]
    failed_keys: dict[str, str]


def _add_faces(
    builder: GlbBuilder,
    mesh: int,
    faces: Sequence[FaceMesh],
    *,
    material: int,
) -> None:
    positions: list[float] = []
    normals: list[float] = []
    indices: list[int] = []
    ranges: list[list[int]] = []
    for face in faces:
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
        material=material,
        extras={"entityKind": "face", "ranges": ranges},
    )


def _add_shape_mesh(
    builder: GlbBuilder,
    mesh: int,
    shape_mesh: ShapeMesh,
    *,
    face_material: int,
    face_materials: dict[int, int],
    edge_material: int | None,
) -> None:
    """Append one body's faces (one primitive per material) and edges (one)."""
    if shape_mesh.faces:
        groups: dict[int, list[FaceMesh]] = {}
        for face in shape_mesh.faces:
            material = face_materials.get(face.pick_id, face_material)
            groups.setdefault(material, []).append(face)
        for material in sorted(groups):
            _add_faces(builder, mesh, groups[material], material=material)
    if shape_mesh.edges and edge_material is not None:
        positions: list[float] = []
        indices: list[int] = []
        ranges: list[list[int]] = []
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
    body_colours: dict[str, Rgba] | None = None,
    face_colours: dict[str, dict[int, Rgba]] | None = None,
) -> GlbResult:
    """Tessellate each body once and place the nodes of the tree.

    ``nodes`` are in depth-first order with ``parent`` indices into the same
    sequence; ``shapes`` maps a body key to its shape. ``body_colours`` gives
    the colour of a body's faces, ``face_colours`` the colour of single faces
    by their ``TopExp::MapShapes`` index; one material is written per colour.
    """
    builder = GlbBuilder(
        generator=generator,
        root_name=root_name,
        length_unit_to_m=0.001,
        up_axis="z",
        root_extras=root_extras,
    )
    materials: dict[Rgba, int] = {FACE_COLOR: builder.add_material("faces", FACE_COLOR)}
    edge_material = (
        builder.add_material("edges", EDGE_COLOR, unlit=True) if include_edges else None
    )

    def material_for(colour: Rgba) -> int:
        material = materials.get(colour)
        if material is None:
            red, green, blue, _alpha = colour
            material = builder.add_material(
                f"colour {red:.3f} {green:.3f} {blue:.3f}", colour
            )
            materials[colour] = material
        return material

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
        except Exception as exc:  # noqa: BLE001 - OCCT failures are reported per body
            failed[key] = f"{type(exc).__name__}: {exc}"
            continue
        if not shape_mesh.faces and not shape_mesh.edges:
            failed[key] = "empty tessellation"
            continue
        body_colour = (body_colours or {}).get(key)
        face_material = (
            materials[FACE_COLOR] if body_colour is None else material_for(body_colour)
        )
        face_materials = {
            pick_id: material_for(colour)
            for pick_id, colour in (face_colours or {}).get(key, {}).items()
        }
        mesh = builder.add_mesh(key)
        _add_shape_mesh(
            builder,
            mesh,
            shape_mesh,
            face_material=face_material,
            face_materials=face_materials,
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
        material_count=len(materials) + (0 if edge_material is None else 1),
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
    "Rgba",
    "brep_bytes",
    "build_glb",
    "diagonal",
    "face_pick_ids",
    "location_from_matrix",
    "matmul",
    "merge_bounds",
    "shape_bounds",
    "transform_bounds",
    "transform_point",
]
