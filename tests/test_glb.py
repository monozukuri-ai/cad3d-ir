from __future__ import annotations

import json
import struct

import pytest

from cad3d_ir.mesh import (
    GlbBuilder,
    drop_degenerate_triangles,
    extend_bounds,
    flat_normals,
    validate_glb_bytes,
    vertex_normals,
)
from cad3d_ir.mesh.glb import gltf_matrix, root_conversion_matrix

_UNIT_SQUARE = [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 1.0, 1.0, 0.0, 0.0, 1.0, 0.0]
_TWO_TRIANGLES = [0, 1, 2, 0, 2, 3]


def _json_chunk(payload: bytes) -> dict:
    length = struct.unpack_from("<I", payload, 12)[0]
    return json.loads(payload[20 : 20 + length].decode("utf-8"))


def test_vertex_normals_are_unit_length_and_orientable() -> None:
    normals = vertex_normals(_UNIT_SQUARE, _TWO_TRIANGLES)
    assert normals == [0.0, 0.0, 1.0] * 4
    assert vertex_normals(_UNIT_SQUARE, [0, 2, 1, 0, 3, 2]) == [0.0, 0.0, -1.0] * 4
    # An unreferenced vertex still receives a finite unit normal.
    assert vertex_normals(_UNIT_SQUARE, [0, 1, 2])[9:] == [0.0, 0.0, 1.0]


def test_flat_normals_and_degenerate_filtering() -> None:
    soup = [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0] + [0.0, 0.0, 0.0] * 3
    kept, dropped = drop_degenerate_triangles(soup)
    assert dropped == 1
    assert flat_normals(kept) == [0.0, 0.0, 1.0] * 3
    with pytest.raises(ValueError, match="nine coordinates"):
        flat_normals([0.0] * 10)


def test_extend_bounds_grows_box() -> None:
    bounds = extend_bounds(None, [1.0, 2.0, 3.0, -1.0, 5.0, 0.5])
    assert bounds == ((-1.0, 2.0, 0.5), (1.0, 5.0, 3.0))
    assert extend_bounds(bounds, [10.0, -10.0, 0.0]) == (
        (-1.0, -10.0, 0.0),
        (10.0, 5.0, 3.0),
    )
    assert extend_bounds(bounds, []) == bounds


def test_root_conversion_matrix_maps_z_up_millimetres_to_gltf() -> None:
    matrix = root_conversion_matrix(0.001, "z")
    assert matrix is not None
    # Column-major: columns are the images of the X, Y, Z basis vectors.
    assert matrix[0:4] == [0.001, 0.0, 0.0, 0.0]
    assert matrix[4:8] == [0.0, 0.0, -0.001, 0.0]
    assert matrix[8:12] == [0.0, 0.001, 0.0, 0.0]
    assert matrix[12:16] == [0.0, 0.0, 0.0, 1.0]
    assert root_conversion_matrix(1.0, "y") is None
    assert gltf_matrix(
        (
            (1.0, 2.0, 3.0, 4.0),
            (5.0, 6.0, 7.0, 8.0),
            (9.0, 10.0, 11.0, 12.0),
            (0.0, 0.0, 0.0, 1.0),
        )
    ) == [
        1.0,
        5.0,
        9.0,
        0.0,
        2.0,
        6.0,
        10.0,
        0.0,
        3.0,
        7.0,
        11.0,
        0.0,
        4.0,
        8.0,
        12.0,
        1.0,
    ]


def test_builder_writes_shared_meshes_lines_and_node_tree() -> None:
    builder = GlbBuilder(generator="test", root_name="package")
    faces = builder.add_material("faces", (0.2, 0.5, 0.8, 1.0))
    edges = builder.add_material("edges", (0.0, 0.0, 0.0, 1.0), unlit=True)
    mesh = builder.add_mesh("square", extras={"id": "body-square"})
    builder.add_triangles(
        mesh,
        positions=_UNIT_SQUARE,
        indices=_TWO_TRIANGLES,
        normals=vertex_normals(_UNIT_SQUARE, _TWO_TRIANGLES),
        material=faces,
        extras={"entityKind": "face", "pickId": 1},
    )
    builder.add_lines(
        mesh,
        positions=[0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 1.0, 1.0, 0.0],
        strip=True,
        material=edges,
        extras={"entityKind": "edge", "pickId": 2},
    )
    builder.add_lines(mesh, positions=[0.0, 0.0, 0.0, 0.0, 0.0, 5.0])
    empty = builder.add_mesh("unused")
    assembly = builder.add_node("assembly", extras={"id": "occurrence-root"})
    first = builder.add_node("square:1", mesh=mesh, parent=assembly)
    second = builder.add_node(
        "square:2",
        mesh=mesh,
        parent=assembly,
        matrix=(
            (1.0, 0.0, 0.0, 20.0),
            (0.0, 1.0, 0.0, 0.0),
            (0.0, 0.0, 1.0, 0.0),
            (0.0, 0.0, 0.0, 1.0),
        ),
    )
    builder.add_node("empty", mesh=empty)

    assert builder.triangle_count == 2
    assert builder.line_segment_count == 3
    assert builder.bounds == ((0.0, 0.0, 0.0), (1.0, 1.0, 5.0))

    payload = builder.build()
    report = validate_glb_bytes(payload)
    assert report.errors == ()
    assert report.valid
    assert (report.node_count, report.mesh_count, report.primitive_count) == (5, 1, 3)
    assert (report.triangle_count, report.line_segment_count) == (2, 3)

    document = _json_chunk(payload)
    assert document["asset"] == {"generator": "test", "version": "2.0"}
    assert document["nodes"][0]["children"] == [assembly, 4]
    assert document["nodes"][0]["matrix"] == root_conversion_matrix(0.001, "z")
    assert document["nodes"][0]["extras"]["cad3d_ir"]["source_up_axis"] == "z"
    assert document["nodes"][assembly]["children"] == [first, second]
    assert document["nodes"][first]["mesh"] == 0
    assert document["nodes"][second]["mesh"] == 0
    assert document["nodes"][second]["matrix"][12:15] == [20.0, 0.0, 0.0]
    assert "matrix" not in document["nodes"][first]
    assert "mesh" not in document["nodes"][4]
    assert document["meshes"][0]["extras"] == {"id": "body-square"}
    primitives = document["meshes"][0]["primitives"]
    assert [primitive["mode"] for primitive in primitives] == [4, 3, 1]
    assert primitives[0]["extras"] == {"entityKind": "face", "pickId": 1}
    position = document["accessors"][primitives[0]["attributes"]["POSITION"]]
    assert position["min"] == [0.0, 0.0, 0.0]
    assert position["max"] == [1.0, 1.0, 0.0]
    assert document["materials"][1]["extensions"] == {"KHR_materials_unlit": {}}
    assert document["extensionsUsed"] == ["KHR_materials_unlit"]
    assert payload == builder.build()


def test_builder_rejects_inconsistent_input() -> None:
    builder = GlbBuilder()
    mesh = builder.add_mesh("mesh")
    with pytest.raises(ValueError, match="complete 3D points"):
        builder.add_triangles(mesh, positions=[0.0, 1.0], indices=[0, 1, 2])
    with pytest.raises(ValueError, match="missing vertices"):
        builder.add_triangles(mesh, positions=_UNIT_SQUARE, indices=[0, 1, 9])
    with pytest.raises(ValueError, match="match positions"):
        builder.add_triangles(
            mesh, positions=_UNIT_SQUARE, indices=[0, 1, 2], normals=[0.0]
        )
    with pytest.raises(ValueError, match="pairs"):
        builder.add_lines(mesh, positions=[0.0] * 9)
    with pytest.raises(ValueError, match="unknown mesh"):
        builder.add_node("node", mesh=5)
    with pytest.raises(ValueError, match="unknown parent"):
        builder.add_node("node", parent=7)
    with pytest.raises(ValueError, match="four channels"):
        builder.add_material("bad", (2.0, 0.0, 0.0, 1.0))
    with pytest.raises(ValueError, match="length_unit_to_m"):
        GlbBuilder(length_unit_to_m=0.0)
    limited = GlbBuilder(max_binary_bytes=16)
    limited_mesh = limited.add_mesh("mesh")
    with pytest.raises(OverflowError):
        limited.add_triangles(limited_mesh, positions=_UNIT_SQUARE, indices=[0, 1, 2])


def test_empty_builder_still_produces_a_valid_glb() -> None:
    payload = GlbBuilder(length_unit_to_m=1.0, up_axis="y").build()
    report = validate_glb_bytes(payload)
    assert report.valid, report.errors
    assert report.node_count == 1 and report.mesh_count == 0
    document = _json_chunk(payload)
    assert "matrix" not in document["nodes"][0]
    assert "meshes" not in document


def test_validator_reports_corruption() -> None:
    builder = GlbBuilder()
    mesh = builder.add_mesh("mesh")
    builder.add_triangles(mesh, positions=_UNIT_SQUARE, indices=_TWO_TRIANGLES)
    payload = bytearray(builder.build())
    assert validate_glb_bytes(bytes(payload)).valid
    payload[0:4] = b"ABCD"
    report = validate_glb_bytes(bytes(payload))
    assert not report.valid
    assert "GLB magic is invalid" in report.errors
    assert not validate_glb_bytes(b"glTF").valid
