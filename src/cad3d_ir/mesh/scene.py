"""Read back a visualization GLB: nodes, placements and primitives.

This is the inverse of :class:`cad3d_ir.mesh.GlbBuilder` for the layout it
writes (one embedded buffer, ``float32`` ``VEC3`` positions and normals,
integer ``SCALAR`` indices, TRIANGLES / LINES / LINE_STRIP primitives). The
reader is what exporters build on: a placed triangle only exists once the
node tree has been walked and every node matrix applied, and the GLB is the
only place where adapters hand that geometry over.

Coordinates come back in **package units** (millimetres and Z-up for every
adapter in this repository). A GLB written by cad3d-ir marks its root with
``extras.cad3d_ir``; that root's unit and up-axis conversion is recognised and
skipped, so meshes and node matrices are read as the adapter wrote them. A GLB
from another writer is taken to follow glTF conventions (metres, Y-up) and is
converted to millimetres, Z-up.
"""

from __future__ import annotations

import json
import struct
from array import array
from collections.abc import Iterator
from dataclasses import dataclass
from math import sqrt
from typing import Any, Final

from cad3d_ir.mesh.geometry import Bounds, extend_bounds
from cad3d_ir.mesh.glb import (
    _BIN_CHUNK,
    _GLB_MAGIC,
    _JSON_CHUNK,
    _MODE_LINE_STRIP,
    _MODE_LINES,
    _MODE_TRIANGLES,
    _native_array,
    _split_chunks,
)
from cad3d_ir.model.common import IDENTITY_MATRIX_4, Matrix4

MODE_LINES: Final = _MODE_LINES
MODE_LINE_STRIP: Final = _MODE_LINE_STRIP
MODE_TRIANGLES: Final = _MODE_TRIANGLES

_COMPONENT_TYPECODES: Final = {
    5120: "b",
    5121: "B",
    5122: "h",
    5123: "H",
    5125: "I",
    5126: "f",
}
_COMPONENT_SIZES: Final = {5120: 1, 5121: 1, 5122: 2, 5123: 2, 5125: 4, 5126: 4}
_TYPE_WIDTHS: Final = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}
_INDEX_COMPONENTS: Final = frozenset({5121, 5123, 5125})
_FLOAT: Final = 5126

# glTF space (metres, Y-up) back to package space (millimetres, Z-up):
# x = 1000 X, y = -1000 Z, z = 1000 Y. The inverse of GlbBuilder's root matrix.
_GLTF_TO_MM_Z_UP: Final[Matrix4] = (
    (1000.0, 0.0, 0.0, 0.0),
    (0.0, 0.0, -1000.0, 0.0),
    (0.0, 1000.0, 0.0, 0.0),
    (0.0, 0.0, 0.0, 1.0),
)


class GlbReadError(ValueError):
    """Raised when a GLB cannot be read back as a mesh scene."""


@dataclass(frozen=True, slots=True)
class ScenePrimitive:
    """One primitive of a mesh, in the mesh's local coordinates."""

    mode: int
    positions: array
    """Flat ``x, y, z`` ``float32`` values (typecode ``f``)."""
    normals: array | None
    indices: array
    """``uint32`` indices (typecode ``I``), three per triangle or as drawn."""
    vertex_set: int
    """The POSITION accessor index: primitives sharing it share vertices."""
    material: int | None = None
    extras: dict[str, Any] | None = None

    @property
    def vertex_count(self) -> int:
        return len(self.positions) // 3

    @property
    def triangle_count(self) -> int:
        return len(self.indices) // 3 if self.mode == MODE_TRIANGLES else 0

    @property
    def line_segment_count(self) -> int:
        if self.mode == MODE_LINES:
            return len(self.indices) // 2
        if self.mode == MODE_LINE_STRIP:
            return max(len(self.indices) - 1, 0)
        return 0


@dataclass(frozen=True, slots=True)
class SceneMesh:
    name: str
    primitives: tuple[ScenePrimitive, ...]
    extras: dict[str, Any] | None = None

    @property
    def triangle_count(self) -> int:
        return sum(primitive.triangle_count for primitive in self.primitives)


@dataclass(frozen=True, slots=True)
class SceneNode:
    """A node with its world matrix in package coordinates."""

    index: int
    name: str
    matrix: Matrix4
    mesh: int | None = None
    parent: int | None = None
    depth: int = 0
    extras: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class PlacedVertexSet:
    """A vertex set of one node placement, transformed to world coordinates.

    ``positions`` and ``normals`` are in package units after the node's world
    matrix; ``triangles`` and ``lines`` are the primitives indexing into it.
    """

    node: SceneNode
    mesh: SceneMesh
    vertex_set: int
    positions: array
    normals: array | None
    triangles: tuple[ScenePrimitive, ...]
    lines: tuple[ScenePrimitive, ...]

    @property
    def vertex_count(self) -> int:
        return len(self.positions) // 3

    @property
    def triangle_count(self) -> int:
        return sum(primitive.triangle_count for primitive in self.triangles)


@dataclass(frozen=True, slots=True)
class MeshScene:
    """The node tree and meshes read from a GLB, in package coordinates."""

    nodes: tuple[SceneNode, ...]
    meshes: tuple[SceneMesh, ...]
    materials: tuple[dict[str, Any], ...] = ()
    length_unit_to_m: float = 0.001
    up_axis: str = "z"
    generator: str | None = None
    from_cad3d_ir: bool = True
    """Whether the root carried ``extras.cad3d_ir`` (otherwise converted)."""

    # -- counts over placements (a mesh placed twice counts twice) ----------

    def placements(self) -> Iterator[tuple[SceneNode, SceneMesh]]:
        """Yield every node that places a mesh, in traversal order."""
        for node in self.nodes:
            if node.mesh is not None:
                yield node, self.meshes[node.mesh]

    @property
    def placement_count(self) -> int:
        return sum(1 for _ in self.placements())

    @property
    def placed_triangle_count(self) -> int:
        return sum(mesh.triangle_count for _, mesh in self.placements())

    @property
    def placed_line_segment_count(self) -> int:
        return sum(
            primitive.line_segment_count
            for _, mesh in self.placements()
            for primitive in mesh.primitives
        )

    @property
    def placed_vertex_count(self) -> int:
        """Vertices of the sets that triangle primitives use, per placement."""
        total = 0
        for _, mesh in self.placements():
            seen: set[int] = set()
            for primitive in mesh.primitives:
                if (
                    primitive.mode == MODE_TRIANGLES
                    and primitive.vertex_set not in seen
                ):
                    seen.add(primitive.vertex_set)
                    total += primitive.vertex_count
        return total

    def placed_vertex_sets(self, *, lines: bool = True) -> Iterator[PlacedVertexSet]:
        """Yield each placement's vertex sets with world coordinates.

        Sets used only by line primitives are skipped unless ``lines`` is true.
        Positions are transformed by the node's world matrix, normals by its
        inverse transpose and renormalised (dropped if the matrix is singular).
        """
        for node, mesh in self.placements():
            groups: dict[int, tuple[list[ScenePrimitive], list[ScenePrimitive]]] = {}
            for primitive in mesh.primitives:
                triangles, strokes = groups.setdefault(primitive.vertex_set, ([], []))
                if primitive.mode == MODE_TRIANGLES:
                    triangles.append(primitive)
                else:
                    strokes.append(primitive)
            for vertex_set, (triangles, strokes) in groups.items():
                if not triangles and not (lines and strokes):
                    continue
                sample = (triangles or strokes)[0]
                positions = transform_positions(sample.positions, node.matrix)
                normals = (
                    transform_normals(sample.normals, node.matrix)
                    if sample.normals is not None
                    else None
                )
                yield PlacedVertexSet(
                    node=node,
                    mesh=mesh,
                    vertex_set=vertex_set,
                    positions=positions,
                    normals=normals,
                    triangles=tuple(triangles),
                    lines=tuple(strokes) if lines else (),
                )

    def bounds(self) -> Bounds | None:
        """World bounds of every placed vertex set used by triangles or lines."""
        bounds: Bounds | None = None
        for placed in self.placed_vertex_sets(lines=True):
            bounds = extend_bounds(bounds, placed.positions)
        return bounds


# -- matrices ----------------------------------------------------------------


def multiply(left: Matrix4, right: Matrix4) -> Matrix4:
    """Return ``left · right`` for row-major matrices acting on column vectors."""
    return tuple(  # type: ignore[return-value]
        tuple(
            sum(left[row][k] * right[k][column] for k in range(4))
            for column in range(4)
        )
        for row in range(4)
    )


def matrix_from_gltf(values: list[float]) -> Matrix4:
    """Convert glTF's column-major 16-list to a row-major cad3d-ir matrix."""
    if len(values) != 16:
        raise GlbReadError("node matrix must have 16 values")
    return tuple(  # type: ignore[return-value]
        tuple(float(values[4 * column + row]) for column in range(4))
        for row in range(4)
    )


def matrix_from_trs(
    translation: list[float] | None,
    rotation: list[float] | None,
    scale: list[float] | None,
) -> Matrix4:
    """Compose glTF ``translation``, ``rotation`` (x, y, z, w) and ``scale``."""
    tx, ty, tz = (float(v) for v in (translation or [0.0, 0.0, 0.0]))
    qx, qy, qz, qw = (float(v) for v in (rotation or [0.0, 0.0, 0.0, 1.0]))
    sx, sy, sz = (float(v) for v in (scale or [1.0, 1.0, 1.0]))
    xx, yy, zz = qx * qx, qy * qy, qz * qz
    xy, xz, yz = qx * qy, qx * qz, qy * qz
    wx, wy, wz = qw * qx, qw * qy, qw * qz
    r00, r01, r02 = 1.0 - 2.0 * (yy + zz), 2.0 * (xy - wz), 2.0 * (xz + wy)
    r10, r11, r12 = 2.0 * (xy + wz), 1.0 - 2.0 * (xx + zz), 2.0 * (yz - wx)
    r20, r21, r22 = 2.0 * (xz - wy), 2.0 * (yz + wx), 1.0 - 2.0 * (xx + yy)
    return (
        (r00 * sx, r01 * sy, r02 * sz, tx),
        (r10 * sx, r11 * sy, r12 * sz, ty),
        (r20 * sx, r21 * sy, r22 * sz, tz),
        (0.0, 0.0, 0.0, 1.0),
    )


def is_identity(matrix: Matrix4) -> bool:
    return matrix == IDENTITY_MATRIX_4


def transform_positions(positions: array, matrix: Matrix4) -> array:
    """Apply ``matrix`` to flat ``x, y, z`` positions; identity returns the input."""
    if is_identity(matrix):
        return positions
    (m00, m01, m02, m03), (m10, m11, m12, m13), (m20, m21, m22, m23), _ = matrix
    out = array("f")
    extend = out.extend
    values = iter(positions)
    for x, y, z in zip(values, values, values, strict=True):
        extend(
            (
                m00 * x + m01 * y + m02 * z + m03,
                m10 * x + m11 * y + m12 * z + m13,
                m20 * x + m21 * y + m22 * z + m23,
            )
        )
    return out


def normal_matrix(matrix: Matrix4) -> tuple[tuple[float, ...], ...] | None:
    """Inverse transpose of the upper-left 3x3, or ``None`` when singular."""
    (a, b, c, _), (d, e, f, _), (g, h, i, _), _ = matrix
    cofactors = (
        (e * i - f * h, -(d * i - f * g), d * h - e * g),
        (-(b * i - c * h), a * i - c * g, -(a * h - b * g)),
        (b * f - c * e, -(a * f - c * d), a * e - b * d),
    )
    det = a * cofactors[0][0] + b * cofactors[0][1] + c * cofactors[0][2]
    if det == 0.0:
        return None
    # inverse = adjugate / det, where adjugate = transpose(cofactors);
    # the inverse transpose is therefore cofactors / det.
    return tuple(tuple(value / det for value in row) for row in cofactors)


def transform_normals(normals: array, matrix: Matrix4) -> array | None:
    """Transform unit normals by the inverse transpose and renormalise."""
    if is_identity(matrix):
        return normals
    rows = normal_matrix(matrix)
    if rows is None:
        return None
    (n00, n01, n02), (n10, n11, n12), (n20, n21, n22) = rows
    out = array("f")
    extend = out.extend
    values = iter(normals)
    for x, y, z in zip(values, values, values, strict=True):
        nx = n00 * x + n01 * y + n02 * z
        ny = n10 * x + n11 * y + n12 * z
        nz = n20 * x + n21 * y + n22 * z
        length = sqrt(nx * nx + ny * ny + nz * nz)
        if length == 0.0:
            extend((0.0, 0.0, 1.0))
        else:
            extend((nx / length, ny / length, nz / length))
    return out


# -- reading -------------------------------------------------------------------


def _parse_container(payload: bytes) -> tuple[dict[str, Any], bytes]:
    if len(payload) < 20:
        raise GlbReadError("GLB is shorter than its header and first chunk")
    magic, version, declared_length = struct.unpack_from("<4sII", payload, 0)
    if magic != _GLB_MAGIC:
        raise GlbReadError("not a GLB: magic bytes are invalid")
    if version != 2:
        raise GlbReadError(f"unsupported GLB version {version}")
    if declared_length != len(payload):
        raise GlbReadError("GLB declared length differs from its byte length")
    errors: list[str] = []
    chunks = _split_chunks(payload, errors)
    if errors:
        raise GlbReadError(errors[0])
    if not chunks or chunks[0][0] != _JSON_CHUNK:
        raise GlbReadError("GLB first chunk must be JSON")
    try:
        document = json.loads(chunks[0][1].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GlbReadError(f"GLB JSON is invalid: {error}") from error
    if not isinstance(document, dict):
        raise GlbReadError("GLB JSON root must be an object")
    binary = b""
    if len(chunks) > 1:
        if chunks[1][0] != _BIN_CHUNK or len(chunks) > 2:
            raise GlbReadError(
                "GLB must contain one JSON chunk and at most one BIN chunk"
            )
        binary = chunks[1][1]
    return document, binary


class _Accessors:
    def __init__(self, document: dict[str, Any], binary: bytes) -> None:
        buffers = document.get("buffers")
        if not isinstance(buffers, list) or len(buffers) != 1:
            raise GlbReadError("GLB must declare exactly one buffer")
        if "uri" in buffers[0]:
            raise GlbReadError("GLB buffer must be embedded, not an external URI")
        self._views = document.get("bufferViews", [])
        self._accessors = document.get("accessors", [])
        self._binary = binary
        self._cache: dict[int, array] = {}

    def read(self, index: Any, *, name: str) -> tuple[array, dict[str, Any]]:
        if not isinstance(index, int) or not 0 <= index < len(self._accessors):
            raise GlbReadError(f"{name} references an unknown accessor")
        accessor = self._accessors[index]
        if index in self._cache:
            return self._cache[index], accessor
        component = accessor.get("componentType")
        typecode = _COMPONENT_TYPECODES.get(component)
        width = _TYPE_WIDTHS.get(accessor.get("type"))
        count = accessor.get("count")
        if typecode is None or width is None or not isinstance(count, int) or count < 0:
            raise GlbReadError(f"{name} accessor uses an unsupported layout")
        if "sparse" in accessor:
            raise GlbReadError(f"{name} accessor is sparse, which is not supported")
        view_index = accessor.get("bufferView")
        if not isinstance(view_index, int) or not 0 <= view_index < len(self._views):
            raise GlbReadError(f"{name} accessor references an unknown bufferView")
        view = self._views[view_index]
        size = _COMPONENT_SIZES[component]
        stride = view.get("byteStride")
        if stride not in (None, 0, width * size):
            raise GlbReadError(
                f"{name} accessor is interleaved, which is not supported"
            )
        offset = int(view.get("byteOffset", 0)) + int(accessor.get("byteOffset", 0))
        length = count * width * size
        if offset < 0 or offset + length > len(self._binary):
            raise GlbReadError(f"{name} accessor exceeds the BIN chunk")
        values = _native_array(typecode, self._binary[offset : offset + length])
        self._cache[index] = values
        return values, accessor


def _read_primitive(
    primitive: dict[str, Any], accessors: _Accessors, *, mesh_name: str
) -> ScenePrimitive:
    mode = primitive.get("mode", MODE_TRIANGLES)
    if mode not in (MODE_TRIANGLES, MODE_LINES, MODE_LINE_STRIP):
        raise GlbReadError(
            f"mesh {mesh_name!r} uses primitive mode {mode}; "
            "only TRIANGLES, LINES and LINE_STRIP are supported"
        )
    attributes = primitive.get("attributes")
    if not isinstance(attributes, dict) or "POSITION" not in attributes:
        raise GlbReadError(f"mesh {mesh_name!r} has a primitive without POSITION")
    positions, accessor = accessors.read(attributes["POSITION"], name="POSITION")
    if accessor.get("componentType") != _FLOAT or accessor.get("type") != "VEC3":
        raise GlbReadError("POSITION must be float32 VEC3")
    normals: array | None = None
    if "NORMAL" in attributes:
        normals, accessor = accessors.read(attributes["NORMAL"], name="NORMAL")
        if accessor.get("componentType") != _FLOAT or accessor.get("type") != "VEC3":
            raise GlbReadError("NORMAL must be float32 VEC3")
        if len(normals) != len(positions):
            raise GlbReadError("NORMAL count differs from POSITION count")
    vertex_count = len(positions) // 3
    if "indices" in primitive:
        raw, accessor = accessors.read(primitive["indices"], name="indices")
        if accessor.get("componentType") not in _INDEX_COMPONENTS:
            raise GlbReadError("indices must be uint8, uint16 or uint32 SCALAR")
        indices = raw if raw.typecode == "I" else array("I", raw)
    else:
        indices = array("I", range(vertex_count))
    if indices and max(indices) >= vertex_count:
        raise GlbReadError(f"mesh {mesh_name!r} indices reference missing vertices")
    if mode == MODE_TRIANGLES and len(indices) % 3:
        raise GlbReadError(f"mesh {mesh_name!r} triangle indices do not come in threes")
    if mode == MODE_LINES and len(indices) % 2:
        raise GlbReadError(f"mesh {mesh_name!r} line indices do not come in pairs")
    material = primitive.get("material")
    return ScenePrimitive(
        mode=mode,
        positions=positions,
        normals=normals,
        indices=indices,
        vertex_set=int(attributes["POSITION"]),
        material=material if isinstance(material, int) else None,
        extras=primitive.get("extras"),
    )


def _local_matrix(node: dict[str, Any]) -> Matrix4:
    if "matrix" in node:
        return matrix_from_gltf(node["matrix"])
    if any(key in node for key in ("translation", "rotation", "scale")):
        return matrix_from_trs(
            node.get("translation"), node.get("rotation"), node.get("scale")
        )
    return IDENTITY_MATRIX_4


def _scene_roots(document: dict[str, Any], nodes: list[dict[str, Any]]) -> list[int]:
    scenes = document.get("scenes")
    if isinstance(scenes, list) and scenes:
        index = document.get("scene", 0)
        if not isinstance(index, int) or not 0 <= index < len(scenes):
            raise GlbReadError("GLB default scene index is out of range")
        roots = scenes[index].get("nodes", [])
        if not isinstance(roots, list):
            raise GlbReadError("GLB scene nodes must be an array")
        return [int(root) for root in roots]
    children = {
        child
        for node in nodes
        for child in node.get("children", [])
        if isinstance(child, int)
    }
    return [index for index in range(len(nodes)) if index not in children]


def read_glb(payload: bytes) -> MeshScene:
    """Read a GLB into a :class:`MeshScene` in package coordinates.

    Raises :class:`GlbReadError` for anything outside the supported layout
    (external buffers, interleaved or sparse accessors, point / strip / fan
    primitives, non-float positions).
    """
    document, binary = _parse_container(payload)
    accessors = _Accessors(document, binary)
    meshes_json = document.get("meshes", [])
    nodes_json = document.get("nodes", [])
    if not isinstance(meshes_json, list) or not isinstance(nodes_json, list):
        raise GlbReadError("GLB meshes and nodes must be arrays")
    meshes: list[SceneMesh] = []
    for index, mesh in enumerate(meshes_json):
        if not isinstance(mesh, dict):
            raise GlbReadError("GLB mesh must be an object")
        name = str(mesh.get("name") or f"mesh-{index}")
        primitives = mesh.get("primitives", [])
        if not isinstance(primitives, list):
            raise GlbReadError(f"mesh {name!r} primitives must be an array")
        meshes.append(
            SceneMesh(
                name=name,
                primitives=tuple(
                    _read_primitive(primitive, accessors, mesh_name=name)
                    for primitive in primitives
                    if isinstance(primitive, dict)
                ),
                extras=mesh.get("extras"),
            )
        )

    length_unit_to_m = 0.001
    up_axis = "z"
    from_cad3d_ir = False
    nodes: list[SceneNode] = []
    visited: set[int] = set()
    # Depth-first, children in order: (index, parent, parent world, depth, is_root).
    stack: list[tuple[int, int | None, Matrix4, int, bool]] = []
    for root in reversed(_scene_roots(document, nodes_json)):
        stack.append((root, None, IDENTITY_MATRIX_4, 0, True))
    while stack:
        index, parent, parent_world, depth, is_root = stack.pop()
        if not isinstance(index, int) or not 0 <= index < len(nodes_json):
            raise GlbReadError("GLB node references an unknown child")
        if index in visited:
            raise GlbReadError("GLB node tree contains a cycle or a shared node")
        visited.add(index)
        node = nodes_json[index]
        if not isinstance(node, dict):
            raise GlbReadError("GLB node must be an object")
        extras = node.get("extras")
        marker = extras.get("cad3d_ir") if isinstance(extras, dict) else None
        if is_root and isinstance(marker, dict):
            # The conversion root: keep package coordinates, remember the units.
            from_cad3d_ir = True
            unit = marker.get("length_unit_to_m")
            if isinstance(unit, int | float) and unit > 0:
                length_unit_to_m = float(unit)
            axis = marker.get("source_up_axis")
            if axis in ("z", "y"):
                up_axis = str(axis)
            world = IDENTITY_MATRIX_4
        elif is_root:
            world = multiply(_GLTF_TO_MM_Z_UP, _local_matrix(node))
        else:
            local = _local_matrix(node)
            world = (
                parent_world if is_identity(local) else multiply(parent_world, local)
            )
        mesh_index = node.get("mesh")
        if mesh_index is not None and not (
            isinstance(mesh_index, int) and 0 <= mesh_index < len(meshes)
        ):
            raise GlbReadError("GLB node references an unknown mesh")
        nodes.append(
            SceneNode(
                index=index,
                name=str(node.get("name") or f"node-{index}"),
                matrix=world,
                mesh=mesh_index,
                parent=parent,
                depth=depth,
                extras=extras if isinstance(extras, dict) else None,
            )
        )
        children = node.get("children", [])
        if not isinstance(children, list):
            raise GlbReadError("GLB node children must be an array")
        for child in reversed(children):
            stack.append((child, index, world, depth + 1, False))

    asset = document.get("asset")
    generator = asset.get("generator") if isinstance(asset, dict) else None
    materials = document.get("materials", [])
    return MeshScene(
        nodes=tuple(nodes),
        meshes=tuple(meshes),
        materials=tuple(m for m in materials if isinstance(m, dict))
        if isinstance(materials, list)
        else (),
        length_unit_to_m=length_unit_to_m,
        up_axis=up_axis,
        generator=str(generator) if isinstance(generator, str) else None,
        from_cad3d_ir=from_cad3d_ir,
    )


__all__ = [
    "MODE_LINES",
    "MODE_LINE_STRIP",
    "MODE_TRIANGLES",
    "GlbReadError",
    "MeshScene",
    "PlacedVertexSet",
    "SceneMesh",
    "SceneNode",
    "ScenePrimitive",
    "matrix_from_gltf",
    "matrix_from_trs",
    "multiply",
    "normal_matrix",
    "read_glb",
    "transform_normals",
    "transform_positions",
]
