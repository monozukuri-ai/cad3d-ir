from __future__ import annotations

import json
import math
import struct
from array import array
from collections.abc import Callable
from typing import Any

import pytest
from conftest import CUBE_EDGES, CUBE_INDICES, CUBE_POSITIONS, ROT_Z_90, translation

from cad3d_ir.mesh import GlbBuilder, GlbReadError, read_glb
from cad3d_ir.mesh.glb import gltf_matrix
from cad3d_ir.mesh.scene import (
    MODE_LINES,
    MODE_TRIANGLES,
    matrix_from_gltf,
    matrix_from_trs,
    multiply,
    normal_matrix,
    transform_normals,
    transform_positions,
)
from cad3d_ir.model.common import IDENTITY_MATRIX_4


def _rewrite_json(payload: bytes, mutate: Callable[[dict[str, Any]], None]) -> bytes:
    """Return ``payload`` with its JSON chunk changed in place by ``mutate``."""
    length = struct.unpack_from("<I", payload, 12)[0]
    document = json.loads(payload[20 : 20 + length].decode("utf-8"))
    mutate(document)
    json_bytes = json.dumps(document, separators=(",", ":")).encode("utf-8")
    json_bytes += b" " * (-len(json_bytes) % 4)
    rest = payload[20 + length :]
    total = 12 + 8 + len(json_bytes) + len(rest)
    return (
        struct.pack("<4sII", b"glTF", 2, total)
        + struct.pack("<II", len(json_bytes), 0x4E4F534A)
        + json_bytes
        + rest
    )


def _approx(values, expected, tolerance=1e-5) -> None:
    assert len(values) == len(expected)
    for value, want in zip(values, expected, strict=True):
        assert math.isclose(value, want, abs_tol=tolerance), (list(values), expected)


def test_read_glb_round_trips_the_builder_layout(cubes_glb: bytes) -> None:
    scene = read_glb(cubes_glb)

    assert scene.from_cad3d_ir
    assert scene.length_unit_to_m == 0.001
    assert scene.up_axis == "z"
    assert scene.generator == "test"
    root, a, b = scene.nodes
    assert root.name == "fixture" and root.mesh is None and root.depth == 0
    assert root.matrix == IDENTITY_MATRIX_4
    assert a.name == "cube-a" and a.parent == 0 and a.depth == 1 and a.mesh == 0
    assert a.matrix == IDENTITY_MATRIX_4
    assert a.extras == {"id": "occurrence-a"}
    assert b.matrix == translation(20.0, 0.0, 0.0)
    (mesh,) = scene.meshes
    assert mesh.name == "cube" and mesh.extras == {"id": "body-cube"}
    faces, edges = mesh.primitives
    assert faces.mode == MODE_TRIANGLES and edges.mode == MODE_LINES
    assert faces.vertex_count == 8 and faces.triangle_count == 12
    assert faces.normals is not None and len(faces.normals) == 24
    assert faces.material == 0 and faces.extras["entityKind"] == "face"
    assert edges.vertex_set == faces.vertex_set
    assert edges.line_segment_count == 12
    assert list(faces.indices) == CUBE_INDICES
    assert list(edges.indices) == CUBE_EDGES
    assert scene.placement_count == 2
    assert scene.placed_triangle_count == 24
    assert scene.placed_vertex_count == 16
    assert scene.placed_line_segment_count == 24
    assert scene.bounds() == ((0.0, 0.0, 0.0), (30.0, 10.0, 10.0))
    assert len(scene.materials) == 1


def test_placed_vertex_sets_apply_world_matrices(nested_cubes_glb: bytes) -> None:
    scene = read_glb(nested_cubes_glb)

    placed = list(scene.placed_vertex_sets())
    assert [item.node.name for item in placed] == ["cube-a", "cube-b", "cube-c"]
    assert placed[0].positions is scene.meshes[0].primitives[0].positions
    assert list(placed[1].positions[:3]) == [20.0, 0.0, 0.0]
    cube_c = placed[2]
    assert cube_c.node.parent == scene.nodes[2].index
    assert cube_c.node.depth == 2
    assert cube_c.node.matrix == multiply(translation(20.0, 20.0, 0.0), ROT_Z_90)
    # Vertex 1 (10, 0, 0) rotates to (0, 10, 0) and moves by (20, 20, 0).
    _approx(cube_c.positions[3:6], [20.0, 30.0, 0.0])
    assert cube_c.normals is not None
    # Its (area-weighted) corner normal turns with the placement: (x, y) -> (-y, x).
    nx, ny, nz = scene.meshes[0].primitives[0].normals[3:6]  # type: ignore[index]
    _approx(cube_c.normals[3:6], [-ny, nx, nz])
    assert math.isclose(sum(v * v for v in cube_c.normals[3:6]), 1.0, abs_tol=1e-5)
    assert len(cube_c.triangles) == 1 and len(cube_c.lines) == 1
    assert scene.placed_triangle_count == 36
    assert scene.bounds() == ((0.0, 0.0, 0.0), (30.0, 30.0, 10.0))


def test_line_only_vertex_sets_are_skipped_without_lines() -> None:
    builder = GlbBuilder(root_name="wire")
    faces = builder.add_mesh("faces")
    builder.add_triangles(faces, positions=CUBE_POSITIONS[:9], indices=[0, 1, 2])
    wire = builder.add_mesh("wire")
    builder.add_lines(wire, positions=[0.0, 0.0, 0.0, 0.0, 0.0, 50.0])
    builder.add_node("solid", mesh=faces)
    builder.add_node("edges", mesh=wire)

    scene = read_glb(builder.build())

    assert scene.placed_vertex_count == 3
    assert [p.node.name for p in scene.placed_vertex_sets(lines=False)] == ["solid"]
    assert [p.node.name for p in scene.placed_vertex_sets()] == ["solid", "edges"]
    assert scene.bounds() == ((0.0, 0.0, 0.0), (10.0, 10.0, 50.0))


def test_foreign_glb_is_converted_from_gltf_space(cubes_glb: bytes) -> None:
    def strip_marker(document: dict[str, Any]) -> None:
        del document["nodes"][0]["extras"]["cad3d_ir"]

    scene = read_glb(_rewrite_json(cubes_glb, strip_marker))

    assert not scene.from_cad3d_ir
    low, high = scene.bounds()  # type: ignore[misc]
    _approx(low, [0.0, 0.0, 0.0], 1e-6)
    _approx(high, [30.0, 10.0, 10.0], 1e-6)
    (_, moved) = list(scene.placements())[1]
    assert moved.name == "cube"
    placed = list(scene.placed_vertex_sets())
    _approx(placed[1].positions[3:6], [30.0, 0.0, 0.0], 1e-6)


def _custom_glb(
    nodes: list[dict[str, Any]],
    primitives: list[dict[str, Any]],
    binary: bytes,
    views: list[dict[str, Any]],
    accessors: list[dict[str, Any]],
) -> bytes:  # noqa: E501
    document = {
        "asset": {"version": "2.0", "generator": "hand"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": nodes,
        "meshes": [{"name": "tri", "primitives": primitives}],
        "buffers": [{"byteLength": len(binary)}],
        "bufferViews": views,
        "accessors": accessors,
    }
    json_bytes = json.dumps(document, separators=(",", ":")).encode("utf-8")
    json_bytes += b" " * (-len(json_bytes) % 4)
    binary += b"\0" * (-len(binary) % 4)
    total = 12 + 8 + len(json_bytes) + 8 + len(binary)
    return (
        struct.pack("<4sII", b"glTF", 2, total)
        + struct.pack("<II", len(json_bytes), 0x4E4F534A)
        + json_bytes
        + struct.pack("<II", len(binary), 0x004E4942)
        + binary
    )


def test_trs_nodes_uint16_indices_and_non_indexed_primitives() -> None:
    positions = struct.pack("<9f", 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0)
    indices = struct.pack("<3H", 0, 1, 2) + b"\0\0"
    binary = positions + indices
    views = [
        {"buffer": 0, "byteOffset": 0, "byteLength": 36},
        {"buffer": 0, "byteOffset": 36, "byteLength": 6},
    ]
    accessors = [
        {"bufferView": 0, "componentType": 5126, "count": 3, "type": "VEC3"},
        {"bufferView": 1, "componentType": 5123, "count": 3, "type": "SCALAR"},
    ]
    half = math.sqrt(0.5)
    nodes = [
        {
            "name": "root",
            "children": [1],
            "extras": {"cad3d_ir": {"length_unit_to_m": 1.0, "source_up_axis": "y"}},
        },
        {
            "name": "placed",
            "mesh": 0,
            "translation": [1.0, 2.0, 3.0],
            "rotation": [0.0, 0.0, half, half],
        },
    ]
    primitives = [
        {"attributes": {"POSITION": 0}, "indices": 1, "mode": 4},
        {"attributes": {"POSITION": 0}},
    ]

    scene = read_glb(_custom_glb(nodes, primitives, binary, views, accessors))

    assert scene.length_unit_to_m == 1.0 and scene.up_axis == "y"
    indexed, direct = scene.meshes[0].primitives
    assert indexed.indices.typecode == "I" and list(indexed.indices) == [0, 1, 2]
    assert list(direct.indices) == [0, 1, 2] and direct.mode == MODE_TRIANGLES
    (placed,) = list(scene.placed_vertex_sets())
    # (1, 0, 0) rotates 90 degrees about Z to (0, 1, 0), then moves by (1, 2, 3).
    _approx(placed.positions[3:6], [1.0, 3.0, 3.0])
    assert placed.normals is None
    assert scene.placed_triangle_count == 2
    assert scene.placed_vertex_count == 3


def test_read_glb_rejects_unsupported_layouts(cubes_glb: bytes) -> None:
    with pytest.raises(GlbReadError, match="magic"):
        read_glb(b"nope" * 8)
    with pytest.raises(GlbReadError, match="length"):
        read_glb(cubes_glb[:-1])

    def points(document: dict[str, Any]) -> None:
        document["meshes"][0]["primitives"][0]["mode"] = 0

    with pytest.raises(GlbReadError, match="mode 0"):
        read_glb(_rewrite_json(cubes_glb, points))

    def external(document: dict[str, Any]) -> None:
        document["buffers"][0]["uri"] = "data.bin"

    with pytest.raises(GlbReadError, match="embedded"):
        read_glb(_rewrite_json(cubes_glb, external))

    def bad_child(document: dict[str, Any]) -> None:
        document["nodes"][0]["children"].append(99)

    with pytest.raises(GlbReadError, match="unknown child"):
        read_glb(_rewrite_json(cubes_glb, bad_child))


def test_matrix_helpers() -> None:
    placed = multiply(translation(1.0, 2.0, 3.0), ROT_Z_90)
    assert matrix_from_gltf(gltf_matrix(placed)) == placed
    assert matrix_from_trs([1.0, 2.0, 3.0], None, None) == translation(1.0, 2.0, 3.0)
    half = math.sqrt(0.5)
    rotated = matrix_from_trs(None, [0.0, 0.0, half, half], None)
    for row, want in zip(rotated, ROT_Z_90, strict=True):
        _approx(row, want)
    scaled = matrix_from_trs(None, None, [2.0, 1.0, 1.0])
    assert normal_matrix(scaled) == ((0.5, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
    singular = (
        (1.0, 0.0, 0.0, 0.0),
        (0.0, 0.0, 0.0, 0.0),
        (0.0, 0.0, 1.0, 0.0),
        (0.0, 0.0, 0.0, 1.0),
    )
    assert normal_matrix(singular) is None
    normals = array("f", [1.0, 0.0, 0.0])
    assert transform_normals(normals, singular) is None
    positions = array("f", [1.0, 0.0, 0.0])
    assert transform_positions(positions, IDENTITY_MATRIX_4) is positions
    _approx(transform_positions(positions, placed), [1.0, 3.0, 3.0])
    # A normal keeps unit length under non-uniform scale.
    stretched = transform_normals(array("f", [1.0, 1.0, 0.0]), scaled)
    assert stretched is not None
    _approx(stretched, [1.0 / math.sqrt(5.0), 2.0 / math.sqrt(5.0), 0.0])
