"""Deterministic glTF 2.0 binary (GLB) writer and structural validator.

The writer has no third-party dependencies. It targets viewers, not
interchange: every primitive is TRIANGLES, LINES, or LINE_STRIP with float
positions, optional float normals, and uint32 indices in one embedded buffer.

Coordinates are given in the package convention (right-handed, Z-up, package
length units). The root node carries the single conversion to glTF
conventions (metres, Y-up), so meshes and node transforms stay in package
units and nothing else has to know about the swap.

Vertex data is accepted either as flat Python sequences or as packed
little-endian buffers (``bytes``, ``bytearray``, ``memoryview`` or
``array.array``): ``float32`` triples for positions and normals, ``uint32``
for indices. Packed buffers are written without being expanded into Python
objects, which is how native-format adapters hand over a million vertices.
One vertex set registered with :meth:`GlbBuilder.add_vertices` can be shared
by several primitives, so face groups and edge outlines of one body do not
duplicate the positions.
"""

from __future__ import annotations

import json
import struct
import sys
from array import array
from collections.abc import Sequence
from dataclasses import dataclass, field
from math import isfinite
from typing import Any, Final, Literal

from cad3d_ir.mesh.geometry import Bounds, extend_bounds
from cad3d_ir.model.common import Matrix4

GLB_MEDIA_TYPE: Final = "model/gltf-binary"
GLB_ENCODING: Final = "glb-2.0"

_GLB_MAGIC: Final = b"glTF"
_JSON_CHUNK: Final = 0x4E4F534A
_BIN_CHUNK: Final = 0x004E4942
_ARRAY_BUFFER: Final = 34962
_ELEMENT_ARRAY_BUFFER: Final = 34963
_FLOAT: Final = 5126
_UNSIGNED_INT: Final = 5125
_MODE_LINES: Final = 1
_MODE_LINE_STRIP: Final = 3
_MODE_TRIANGLES: Final = 4

ColorRGBA = tuple[float, float, float, float]
FloatArray = Sequence[float] | bytes | bytearray | memoryview | array
"""Flat ``x, y, z, ...`` floats, or their packed little-endian ``float32`` bytes."""
IndexArray = Sequence[int] | bytes | bytearray | memoryview | array
"""Vertex indices, or their packed little-endian ``uint32`` bytes."""

_BUFFER_TYPES: Final = (bytes, bytearray, memoryview)
_BIG_ENDIAN: Final = sys.byteorder == "big"


def _native_array(typecode: str, payload: bytes) -> array:
    """Decode little-endian ``payload`` into a native-order array."""
    values = array(typecode)
    values.frombytes(payload)
    if _BIG_ENDIAN:
        values.byteswap()
    return values


def _array_payload(values: array) -> bytes:
    if _BIG_ENDIAN:
        values = array(values.typecode, values)
        values.byteswap()
    return values.tobytes()


def _float32_payload(values: FloatArray, *, name: str) -> bytes:
    """Return the little-endian ``float32`` bytes of a float sequence or buffer."""
    if isinstance(values, array):
        if values.typecode != "f":
            raise ValueError(f"{name} array must use typecode 'f'")
        return _array_payload(values)
    if isinstance(values, _BUFFER_TYPES):
        payload = bytes(values)
        if len(payload) % 4:
            raise ValueError(f"{name} buffer must hold whole float32 values")
        return payload
    return struct.pack(f"<{len(values)}f", *values)


def _uint32_payload(values: IndexArray, *, name: str) -> bytes:
    """Return the little-endian ``uint32`` bytes of an index sequence or buffer."""
    if isinstance(values, array):
        if values.typecode not in {"I", "L"} or values.itemsize != 4:
            raise ValueError(f"{name} array must hold 32-bit unsigned integers")
        return _array_payload(values)
    if isinstance(values, _BUFFER_TYPES):
        payload = bytes(values)
        if len(payload) % 4:
            raise ValueError(f"{name} buffer must hold whole uint32 values")
        return payload
    if values and min(values) < 0:
        raise ValueError(f"{name} reference missing vertices")
    return struct.pack(f"<{len(values)}I", *values)


def _index_stats(payload: bytes) -> tuple[int, int]:
    """Return the index count and the highest index (``-1`` when empty)."""
    values = _native_array("I", payload)
    return len(values), (max(values) if values else -1)


def _payload_bounds(payload: bytes) -> Bounds:
    values = _native_array("f", payload)
    xs, ys, zs = values[0::3], values[1::3], values[2::3]
    return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))


@dataclass(frozen=True, slots=True)
class _VertexSet:
    position_accessor: int
    normal_accessor: int | None
    count: int


def gltf_matrix(matrix: Matrix4) -> list[float]:
    """Convert a row-major cad3d-ir matrix to glTF's column-major 16-list."""
    return [float(matrix[row][column]) for column in range(4) for row in range(4)]


def root_conversion_matrix(
    length_unit_to_m: float,
    up_axis: Literal["z", "y"],
) -> list[float] | None:
    """Return the glTF root matrix mapping package space to metres and Y-up.

    ``None`` means the identity (package already in metres and Y-up).
    """
    scale = float(length_unit_to_m)
    if up_axis == "z":
        # world = (s*x, s*z, -s*y): rotate -90 degrees about X, then scale.
        rows: Matrix4 = (
            (scale, 0.0, 0.0, 0.0),
            (0.0, 0.0, scale, 0.0),
            (0.0, -scale, 0.0, 0.0),
            (0.0, 0.0, 0.0, 1.0),
        )
    else:
        if scale == 1.0:
            return None
        rows = (
            (scale, 0.0, 0.0, 0.0),
            (0.0, scale, 0.0, 0.0),
            (0.0, 0.0, scale, 0.0),
            (0.0, 0.0, 0.0, 1.0),
        )
    return gltf_matrix(rows)


@dataclass(slots=True)
class GlbBuilder:
    """Append meshes and nodes, then emit one self-contained GLB.

    Node ``0`` is always the root that converts package units and up axis to
    glTF conventions. Nodes added without a parent become its children. A mesh
    may be referenced by several nodes, which is how occurrences of one product
    definition share geometry.
    """

    generator: str = "cad3d-ir"
    root_name: str = "cad3d-ir"
    length_unit_to_m: float = 0.001
    up_axis: Literal["z", "y"] = "z"
    max_binary_bytes: int | None = None
    root_extras: dict[str, Any] | None = None
    _binary: bytearray = field(init=False, default_factory=bytearray, repr=False)
    _buffer_views: list[dict[str, Any]] = field(
        init=False, default_factory=list, repr=False
    )
    _accessors: list[dict[str, Any]] = field(
        init=False, default_factory=list, repr=False
    )
    _meshes: list[dict[str, Any]] = field(init=False, default_factory=list, repr=False)
    _nodes: list[dict[str, Any]] = field(init=False, default_factory=list, repr=False)
    _materials: list[dict[str, Any]] = field(
        init=False, default_factory=list, repr=False
    )
    _vertex_sets: list[_VertexSet] = field(init=False, default_factory=list, repr=False)
    _triangle_count: int = field(init=False, default=0)
    _line_segment_count: int = field(init=False, default=0)
    _vertex_count: int = field(init=False, default=0)
    _bounds: Bounds | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        if isinstance(self.length_unit_to_m, bool) or not isinstance(
            self.length_unit_to_m, int | float
        ):
            raise ValueError("length_unit_to_m must be a finite positive number")
        if not isfinite(self.length_unit_to_m) or self.length_unit_to_m <= 0:
            raise ValueError("length_unit_to_m must be a finite positive number")
        if self.max_binary_bytes is not None and (
            isinstance(self.max_binary_bytes, bool)
            or not isinstance(self.max_binary_bytes, int)
            or self.max_binary_bytes <= 0
        ):
            raise ValueError("max_binary_bytes must be a positive integer or None")
        root: dict[str, Any] = {"name": self.root_name, "children": []}
        matrix = root_conversion_matrix(self.length_unit_to_m, self.up_axis)
        if matrix is not None:
            root["matrix"] = matrix
        extras = {
            "cad3d_ir": {
                "length_unit_to_m": float(self.length_unit_to_m),
                "source_up_axis": self.up_axis,
            }
        }
        if self.root_extras:
            extras.update(self.root_extras)
        root["extras"] = extras
        self._nodes.append(root)

    # -- counters -----------------------------------------------------------

    @property
    def triangle_count(self) -> int:
        return self._triangle_count

    @property
    def line_segment_count(self) -> int:
        return self._line_segment_count

    @property
    def vertex_count(self) -> int:
        return self._vertex_count

    @property
    def bounds(self) -> Bounds | None:
        """Bounds of every position written so far, in package units."""
        return self._bounds

    @property
    def node_count(self) -> int:
        return len(self._nodes)

    @property
    def mesh_count(self) -> int:
        return len(self._meshes)

    @property
    def binary_length(self) -> int:
        return len(self._binary)

    # -- materials, meshes, nodes --------------------------------------------

    def add_material(
        self,
        name: str,
        base_color: ColorRGBA,
        *,
        metallic: float = 0.0,
        roughness: float = 0.7,
        double_sided: bool = True,
        unlit: bool = False,
    ) -> int:
        """Register a PBR material and return its index."""
        if len(base_color) != 4 or any(
            not isfinite(channel) or channel < 0.0 or channel > 1.0
            for channel in base_color
        ):
            raise ValueError("base_color must be four channels in [0, 1]")
        material: dict[str, Any] = {
            "name": name,
            "pbrMetallicRoughness": {
                "baseColorFactor": [float(channel) for channel in base_color],
                "metallicFactor": float(metallic),
                "roughnessFactor": float(roughness),
            },
            "doubleSided": bool(double_sided),
        }
        if base_color[3] < 1.0:
            material["alphaMode"] = "BLEND"
        if unlit:
            material["extensions"] = {"KHR_materials_unlit": {}}
        self._materials.append(material)
        return len(self._materials) - 1

    def add_mesh(self, name: str, *, extras: dict[str, Any] | None = None) -> int:
        """Create an empty mesh and return its index."""
        mesh: dict[str, Any] = {"name": name, "primitives": []}
        if extras:
            mesh["extras"] = dict(extras)
        self._meshes.append(mesh)
        return len(self._meshes) - 1

    def add_node(
        self,
        name: str,
        *,
        mesh: int | None = None,
        parent: int | None = None,
        matrix: Matrix4 | None = None,
        extras: dict[str, Any] | None = None,
    ) -> int:
        """Add a node under ``parent`` (the root by default) and return its index."""
        if mesh is not None and not 0 <= mesh < len(self._meshes):
            raise ValueError(f"unknown mesh index {mesh}")
        parent_index = 0 if parent is None else parent
        if not 0 <= parent_index < len(self._nodes):
            raise ValueError(f"unknown parent node index {parent_index}")
        node: dict[str, Any] = {"name": name}
        if mesh is not None:
            node["mesh"] = mesh
        if matrix is not None:
            values = gltf_matrix(matrix)
            if any(not isfinite(value) for value in values):
                raise ValueError("node matrix must be finite")
            if values != gltf_matrix(_IDENTITY):
                node["matrix"] = values
        if extras:
            node["extras"] = dict(extras)
        self._nodes.append(node)
        index = len(self._nodes) - 1
        self._nodes[parent_index].setdefault("children", []).append(index)
        return index

    # -- vertices and primitives ---------------------------------------------

    def add_vertices(
        self,
        *,
        positions: FloatArray,
        normals: FloatArray | None = None,
    ) -> int:
        """Register a vertex set and return its handle for ``vertices=``.

        ``positions`` (and ``normals``, which must have the same length) are
        flat ``x, y, z`` floats or packed little-endian ``float32`` buffers.
        The set is written once; any number of TRIANGLES, LINES or LINE_STRIP
        primitives may index into it. Its vertices count once towards
        :attr:`vertex_count` and :attr:`bounds`.
        """
        payload = _float32_payload(positions, name="positions")
        if len(payload) % 12 or not payload:
            raise ValueError("positions must hold complete 3D points")
        normal_payload: bytes | None = None
        if normals is not None:
            normal_payload = _float32_payload(normals, name="normals")
            if len(normal_payload) != len(payload):
                raise ValueError("normals must match positions")
        position_accessor = self._add_float_payload(payload, bounds=True)
        normal_accessor = (
            None
            if normal_payload is None
            else self._add_float_payload(normal_payload, bounds=False)
        )
        count = len(payload) // 12
        self._vertex_sets.append(_VertexSet(position_accessor, normal_accessor, count))
        self._vertex_count += count
        low, high = _payload_bounds(payload)
        self._bounds = extend_bounds(self._bounds, [*low, *high])
        return len(self._vertex_sets) - 1

    def add_triangles(
        self,
        mesh: int,
        *,
        indices: IndexArray,
        positions: FloatArray | None = None,
        normals: FloatArray | None = None,
        vertices: int | None = None,
        material: int | None = None,
        extras: dict[str, Any] | None = None,
    ) -> int:
        """Append an indexed TRIANGLES primitive; return its index in the mesh.

        Pass either ``positions`` (with optional ``normals``) to write a vertex
        set for this primitive alone, or ``vertices`` from
        :meth:`add_vertices` to index a shared set. ``indices`` are three per
        triangle, as a sequence or packed ``uint32`` bytes.
        """
        self._check_mesh(mesh)
        vertex_set = self._resolve_vertex_set(
            positions=positions, normals=normals, vertices=vertices
        )
        index_payload = _uint32_payload(indices, name="triangle indices")
        count, highest = _index_stats(index_payload)
        if count % 3 or not count:
            raise ValueError("triangle indices must come in threes")
        if highest >= vertex_set.count:
            raise ValueError("triangle indices reference missing vertices")
        attributes = {"POSITION": vertex_set.position_accessor}
        if vertex_set.normal_accessor is not None:
            attributes["NORMAL"] = vertex_set.normal_accessor
        primitive: dict[str, Any] = {
            "attributes": attributes,
            "indices": self._add_index_payload(index_payload),
            "mode": _MODE_TRIANGLES,
        }
        if material is not None:
            self._check_material(material)
            primitive["material"] = material
        if extras:
            primitive["extras"] = dict(extras)
        self._meshes[mesh]["primitives"].append(primitive)
        self._triangle_count += count // 3
        return len(self._meshes[mesh]["primitives"]) - 1

    def add_lines(
        self,
        mesh: int,
        *,
        positions: FloatArray | None = None,
        indices: IndexArray | None = None,
        vertices: int | None = None,
        strip: bool = False,
        material: int | None = None,
        extras: dict[str, Any] | None = None,
    ) -> int:
        """Append a LINES (index pairs) or LINE_STRIP primitive.

        Without ``indices`` the points are used in order: as one strip, or as
        consecutive segments for LINES (so ``positions`` holds pairs). A shared
        vertex set from :meth:`add_vertices` is used through ``vertices``.
        """
        self._check_mesh(mesh)
        if vertices is None and positions is not None:
            payload = _float32_payload(positions, name="line positions")
            if len(payload) % 12 or len(payload) < 24:
                raise ValueError("line positions need at least two 3D points")
        vertex_set = self._resolve_vertex_set(
            positions=positions, normals=None, vertices=vertices
        )
        if vertex_set.count < 2:
            raise ValueError("line positions need at least two 3D points")
        if indices is None:
            index_payload = _uint32_payload(range(vertex_set.count), name="indices")
        else:
            index_payload = _uint32_payload(indices, name="line indices")
        count, highest = _index_stats(index_payload)
        if not count:
            raise ValueError("line indices must not be empty")
        if highest >= vertex_set.count:
            raise ValueError("line indices reference missing vertices")
        if strip:
            if count < 2:
                raise ValueError("a line strip needs at least two indices")
            segments = count - 1
        else:
            if count % 2 or count < 2:
                raise ValueError("LINES indices must come in pairs")
            segments = count // 2
        primitive: dict[str, Any] = {
            "attributes": {"POSITION": vertex_set.position_accessor},
            "indices": self._add_index_payload(index_payload),
            "mode": _MODE_LINE_STRIP if strip else _MODE_LINES,
        }
        if material is not None:
            self._check_material(material)
            primitive["material"] = material
        if extras:
            primitive["extras"] = dict(extras)
        self._meshes[mesh]["primitives"].append(primitive)
        self._line_segment_count += segments
        return len(self._meshes[mesh]["primitives"]) - 1

    # -- output ------------------------------------------------------------

    def build(self) -> bytes:
        """Serialize the GLB. Meshes without primitives are dropped from nodes."""
        used_meshes = [
            index for index, mesh in enumerate(self._meshes) if mesh["primitives"]
        ]
        remap = {old: new for new, old in enumerate(used_meshes)}
        nodes: list[dict[str, Any]] = []
        for node in self._nodes:
            copy = dict(node)
            mesh_index = copy.pop("mesh", None)
            if mesh_index is not None and mesh_index in remap:
                copy["mesh"] = remap[mesh_index]
            if "children" in copy:
                copy["children"] = list(copy["children"])
                if not copy["children"]:
                    del copy["children"]
            nodes.append(copy)
        document: dict[str, Any] = {
            "asset": {"generator": self.generator, "version": "2.0"},
            "scene": 0,
            "scenes": [{"name": self.root_name, "nodes": [0]}],
            "nodes": nodes,
            "buffers": [{"byteLength": len(self._binary)}],
        }
        if used_meshes:
            document["meshes"] = [self._meshes[index] for index in used_meshes]
        if self._materials:
            document["materials"] = self._materials
            if any("extensions" in material for material in self._materials):
                document["extensionsUsed"] = ["KHR_materials_unlit"]
        if self._buffer_views:
            document["bufferViews"] = self._buffer_views
            document["accessors"] = self._accessors
        json_bytes = json.dumps(
            document,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
        json_bytes += b" " * (-len(json_bytes) % 4)
        binary = bytes(self._binary) + b"\0" * (-len(self._binary) % 4)
        total_length = 12 + 8 + len(json_bytes) + (8 + len(binary) if binary else 0)
        chunks = [
            struct.pack("<4sII", _GLB_MAGIC, 2, total_length),
            struct.pack("<II", len(json_bytes), _JSON_CHUNK),
            json_bytes,
        ]
        if binary:
            chunks.append(struct.pack("<II", len(binary), _BIN_CHUNK))
            chunks.append(binary)
        return b"".join(chunks)

    # -- internals -----------------------------------------------------------

    def _check_mesh(self, mesh: int) -> None:
        if not 0 <= mesh < len(self._meshes):
            raise ValueError(f"unknown mesh index {mesh}")

    def _check_material(self, material: int) -> None:
        if not 0 <= material < len(self._materials):
            raise ValueError(f"unknown material index {material}")

    def _resolve_vertex_set(
        self,
        *,
        positions: FloatArray | None,
        normals: FloatArray | None,
        vertices: int | None,
    ) -> _VertexSet:
        if vertices is None:
            if positions is None:
                raise ValueError("positions or vertices is required")
            return self._vertex_sets[
                self.add_vertices(positions=positions, normals=normals)
            ]
        if positions is not None or normals is not None:
            raise ValueError("positions and normals cannot be combined with vertices")
        if not 0 <= vertices < len(self._vertex_sets):
            raise ValueError(f"unknown vertex set {vertices}")
        return self._vertex_sets[vertices]

    def _add_float_payload(self, payload: bytes, *, bounds: bool) -> int:
        view = self._add_buffer_view(payload, target=_ARRAY_BUFFER)
        accessor: dict[str, Any] = {
            "bufferView": view,
            "componentType": _FLOAT,
            "count": len(payload) // 12,
            "type": "VEC3",
        }
        if bounds:
            # glTF requires POSITION bounds; they are the float32-rounded values.
            low, high = _payload_bounds(payload)
            accessor["min"] = list(low)
            accessor["max"] = list(high)
        self._accessors.append(accessor)
        return len(self._accessors) - 1

    def _add_index_payload(self, payload: bytes) -> int:
        view = self._add_buffer_view(payload, target=_ELEMENT_ARRAY_BUFFER)
        self._accessors.append(
            {
                "bufferView": view,
                "componentType": _UNSIGNED_INT,
                "count": len(payload) // 4,
                "type": "SCALAR",
            }
        )
        return len(self._accessors) - 1

    def _add_buffer_view(self, payload: bytes, *, target: int) -> int:
        padding = -len(self._binary) % 4
        projected = len(self._binary) + padding + len(payload)
        if self.max_binary_bytes is not None and projected > self.max_binary_bytes:
            raise OverflowError("GLB binary buffer exceeds max_binary_bytes")
        if padding:
            self._binary.extend(b"\0" * padding)
        offset = len(self._binary)
        self._binary.extend(payload)
        self._buffer_views.append(
            {
                "buffer": 0,
                "byteLength": len(payload),
                "byteOffset": offset,
                "target": target,
            }
        )
        return len(self._buffer_views) - 1


_IDENTITY: Matrix4 = (
    (1.0, 0.0, 0.0, 0.0),
    (0.0, 1.0, 0.0, 0.0),
    (0.0, 0.0, 1.0, 0.0),
    (0.0, 0.0, 0.0, 1.0),
)


@dataclass(frozen=True, slots=True)
class GlbValidationReport:
    """Structural facts about a GLB produced by :class:`GlbBuilder`."""

    valid: bool
    version: int
    declared_length: int
    binary_length: int
    node_count: int
    mesh_count: int
    primitive_count: int
    triangle_count: int
    line_segment_count: int
    errors: tuple[str, ...] = ()


def _split_chunks(payload: bytes, errors: list[str]) -> list[tuple[int, bytes]]:
    chunks: list[tuple[int, bytes]] = []
    offset = 12
    while offset + 8 <= len(payload):
        length, chunk_type = struct.unpack_from("<II", payload, offset)
        offset += 8
        if length % 4:
            errors.append("GLB chunk length must be four-byte aligned")
        end = offset + length
        if end > len(payload):
            errors.append("GLB chunk extends past the payload")
            break
        chunks.append((chunk_type, payload[offset:end]))
        offset = end
    if offset != len(payload):
        errors.append("GLB contains trailing or truncated chunk bytes")
    return chunks


def validate_glb_bytes(payload: bytes) -> GlbValidationReport:
    """Check the self-contained GLB layout this package writes and reads back."""
    errors: list[str] = []
    version = 0
    declared_length = 0
    document: dict[str, Any] = {}
    binary = b""
    if len(payload) < 20:
        errors.append("GLB is shorter than its header and first chunk")
    else:
        magic, version, declared_length = struct.unpack_from("<4sII", payload, 0)
        if magic != _GLB_MAGIC:
            errors.append("GLB magic is invalid")
        if version != 2:
            errors.append("GLB version must be 2")
        if declared_length != len(payload):
            errors.append("GLB declared length differs from its byte length")
        chunks = _split_chunks(payload, errors)
        if not chunks or chunks[0][0] != _JSON_CHUNK:
            errors.append("GLB first chunk must be JSON")
        if len(chunks) > 2 or (len(chunks) == 2 and chunks[1][0] != _BIN_CHUNK):
            errors.append("GLB must contain one JSON chunk and at most one BIN chunk")
        if chunks and chunks[0][0] == _JSON_CHUNK:
            try:
                decoded = json.loads(chunks[0][1].decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                errors.append(f"GLB JSON is invalid: {error}")
            else:
                if isinstance(decoded, dict):
                    document = decoded
                else:
                    errors.append("GLB JSON root must be an object")
        if len(chunks) == 2 and chunks[1][0] == _BIN_CHUNK:
            binary = chunks[1][1]

    node_count = mesh_count = primitive_count = triangle_count = segment_count = 0
    if document:
        asset = document.get("asset")
        if not isinstance(asset, dict) or asset.get("version") != "2.0":
            errors.append("GLB asset.version must be 2.0")
        buffers = document.get("buffers")
        if not isinstance(buffers, list) or len(buffers) != 1:
            errors.append("GLB must declare exactly one buffer")
        else:
            buffer = buffers[0]
            byte_length = buffer.get("byteLength") if isinstance(buffer, dict) else None
            if not isinstance(byte_length, int) or byte_length > len(binary):
                errors.append("GLB buffer byteLength exceeds the BIN chunk")
            if isinstance(buffer, dict) and "uri" in buffer:
                errors.append("GLB buffer must not use an external URI")
        views = document.get("bufferViews", [])
        accessors = document.get("accessors", [])
        meshes = document.get("meshes", [])
        nodes = document.get("nodes", [])
        if not isinstance(views, list) or not isinstance(accessors, list):
            errors.append("GLB bufferViews and accessors must be arrays")
            views, accessors = [], []
        for view in views:
            offset = view.get("byteOffset", 0) if isinstance(view, dict) else -1
            length = view.get("byteLength") if isinstance(view, dict) else None
            if (
                not isinstance(offset, int)
                or not isinstance(length, int)
                or offset < 0
                or length < 0
                or offset + length > len(binary)
            ):
                errors.append("GLB bufferView exceeds the BIN chunk")
        sizes = {_FLOAT: 4, _UNSIGNED_INT: 4}
        widths = {"SCALAR": 1, "VEC3": 3}
        for accessor in accessors:
            if not isinstance(accessor, dict):
                errors.append("GLB accessor must be an object")
                continue
            view_index = accessor.get("bufferView")
            count = accessor.get("count")
            size = sizes.get(accessor.get("componentType"))
            width = widths.get(accessor.get("type"))
            if not isinstance(view_index, int) or not 0 <= view_index < len(views):
                errors.append("GLB accessor references an unknown bufferView")
                continue
            if not isinstance(count, int) or count <= 0:
                errors.append("GLB accessor count must be positive")
                continue
            if size is None or width is None:
                errors.append("GLB accessor uses an unsupported component/type")
                continue
            view_length = views[view_index].get("byteLength", 0)
            if accessor.get("byteOffset", 0) + count * size * width > view_length:
                errors.append("GLB accessor exceeds its bufferView")
        if not isinstance(nodes, list) or not nodes:
            errors.append("GLB must contain at least the root node")
            nodes = []
        node_count = len(nodes)
        for node in nodes:
            if not isinstance(node, dict):
                errors.append("GLB node must be an object")
                continue
            mesh_index = node.get("mesh")
            if mesh_index is not None and not (
                isinstance(mesh_index, int) and 0 <= mesh_index < len(meshes)
            ):
                errors.append("GLB node references an unknown mesh")
            for child in node.get("children", []):
                if not isinstance(child, int) or not 0 <= child < len(nodes):
                    errors.append("GLB node references an unknown child")
        if not isinstance(meshes, list):
            errors.append("GLB meshes must be an array")
            meshes = []
        mesh_count = len(meshes)
        for mesh in meshes:
            primitives = mesh.get("primitives") if isinstance(mesh, dict) else None
            if not isinstance(primitives, list) or not primitives:
                errors.append("GLB mesh must contain primitives")
                continue
            primitive_count += len(primitives)
            for primitive in primitives:
                mode = primitive.get("mode") if isinstance(primitive, dict) else None
                if mode not in {_MODE_LINES, _MODE_LINE_STRIP, _MODE_TRIANGLES}:
                    errors.append(
                        "GLB primitive mode must be LINES, LINE_STRIP or TRIANGLES"
                    )
                    continue
                attributes = primitive.get("attributes")
                position = (
                    attributes.get("POSITION") if isinstance(attributes, dict) else None
                )
                index_accessor = primitive.get("indices")
                if not isinstance(position, int) or not 0 <= position < len(accessors):
                    errors.append("GLB primitive must reference a POSITION accessor")
                if not isinstance(index_accessor, int) or not (
                    0 <= index_accessor < len(accessors)
                ):
                    errors.append("GLB primitive must reference an index accessor")
                    continue
                count = accessors[index_accessor].get("count", 0)
                if mode == _MODE_TRIANGLES:
                    if count % 3:
                        errors.append(
                            "GLB triangle index count must be a multiple of 3"
                        )
                    triangle_count += count // 3
                elif mode == _MODE_LINES:
                    if count % 2:
                        errors.append("GLB LINES index count must be even")
                    segment_count += count // 2
                else:
                    segment_count += max(count - 1, 0)
        for external_key in ("images", "textures", "animations", "skins"):
            if document.get(external_key):
                errors.append(f"GLB must not contain {external_key}")

    return GlbValidationReport(
        valid=not errors,
        version=version,
        declared_length=declared_length,
        binary_length=len(binary),
        node_count=node_count,
        mesh_count=mesh_count,
        primitive_count=primitive_count,
        triangle_count=triangle_count,
        line_segment_count=segment_count,
        errors=tuple(errors),
    )


__all__ = [
    "GLB_ENCODING",
    "GLB_MEDIA_TYPE",
    "FloatArray",
    "GlbBuilder",
    "GlbValidationReport",
    "IndexArray",
    "gltf_matrix",
    "root_conversion_matrix",
    "validate_glb_bytes",
]
