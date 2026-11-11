"""The glTF helpers the exact B-Rep adapters share."""

from __future__ import annotations

import json
import struct

import pytest
from cad3d_ir.mesh import validate_glb_bytes
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

from cad3d_ir_step.shapes import (
    FACE_COLOR,
    IDENTITY,
    GlbNode,
    brep_bytes,
    build_glb,
    diagonal,
    face_pick_ids,
    location_from_matrix,
    matmul,
    merge_bounds,
    shape_bounds,
    transform_bounds,
)

SHIFT = (
    (1.0, 0.0, 0.0, 100.0),
    (0.0, 1.0, 0.0, 0.0),
    (0.0, 0.0, 1.0, 0.0),
    (0.0, 0.0, 0.0, 1.0),
)


def _json_chunk(payload: bytes) -> dict:
    length = struct.unpack_from("<I", payload, 12)[0]
    return json.loads(payload[20 : 20 + length].decode("utf-8"))


def _build(nodes, shapes, **overrides):
    values = {
        "linear_deflection": 0.1,
        "angular_deflection": 0.3,
        "include_edges": True,
        "generator": "test",
        "root_name": "root",
    }
    return build_glb(nodes, shapes, **{**values, **overrides})


def test_build_glb_shares_one_mesh_between_placements() -> None:
    box = BRepPrimAPI_MakeBox(10.0, 20.0, 30.0).Shape()
    nodes = [
        GlbNode("group", None, IDENTITY, None),
        GlbNode("first", 0, IDENTITY, "box", {"id": "a"}),
        GlbNode("second", 0, SHIFT, "box", {"id": "b"}),
    ]
    result = _build(nodes, {"box": box})
    assert validate_glb_bytes(result.payload).valid
    assert result.mesh_count == 1 and result.triangle_count == 12
    assert result.placed_triangles == 24 and result.failed_keys == {}
    assert result.mesh_triangles == {"box": 12}
    assert result.placed_bounds is not None
    assert result.placed_bounds[1][0] == pytest.approx(110.0)
    assert result.bounds is not None and result.bounds[1][0] == pytest.approx(10.0)
    gltf = _json_chunk(result.payload)
    placed = [node for node in gltf["nodes"] if "mesh" in node]
    assert [node["name"] for node in placed] == ["first", "second"]
    assert {node["mesh"] for node in placed} == {0}
    assert placed[1]["extras"] == {"id": "b"}
    assert len(gltf["meshes"]) == 1


def test_build_glb_writes_one_material_per_colour_and_reports_failures() -> None:
    from OCP.TopoDS import TopoDS_Compound

    box = BRepPrimAPI_MakeBox(1.0, 1.0, 1.0).Shape()
    red = (1.0, 0.0, 0.0, 1.0)
    blue = (0.0, 0.0, 1.0, 1.0)
    nodes = [
        GlbNode("plain", None, IDENTITY, "plain"),
        GlbNode("painted", None, IDENTITY, "painted"),
        GlbNode("empty", None, IDENTITY, "empty"),
    ]
    result = _build(
        nodes,
        {"plain": box, "painted": box, "empty": TopoDS_Compound()},
        include_edges=False,
        body_colours={"painted": red},
        face_colours={"painted": {1: blue}},
    )
    assert result.failed_keys.keys() == {"empty"}
    assert result.line_segment_count == 0
    # The default face colour, the body colour and one face colour.
    assert result.material_count == 3
    colours = [
        tuple(material["pbrMetallicRoughness"]["baseColorFactor"])
        for material in _json_chunk(result.payload)["materials"]
    ]
    assert set(colours) == {FACE_COLOR, red, blue}


def test_bounds_matrices_and_brep_bytes() -> None:
    box = BRepPrimAPI_MakeBox(10.0, 20.0, 30.0).Shape()
    bounds = shape_bounds(box)
    assert bounds is not None
    assert bounds[1] == pytest.approx((10.0, 20.0, 30.0), abs=1e-6)
    assert diagonal(bounds) == pytest.approx((100 + 400 + 900) ** 0.5, abs=1e-5)
    moved = transform_bounds(bounds, SHIFT)
    assert moved[0][0] == pytest.approx(100.0, abs=1e-6)
    assert merge_bounds(bounds, moved)[1][0] == pytest.approx(110.0, abs=1e-6)
    assert merge_bounds(None, bounds) == bounds
    assert matmul(SHIFT, SHIFT)[0][3] == pytest.approx(200.0)
    shifted = shape_bounds(box.Moved(location_from_matrix(SHIFT)))
    assert shifted is not None and shifted[0][0] == pytest.approx(100.0, abs=1e-6)
    assert brep_bytes(box).startswith(b"DBRep_DrawableShape")
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer

    explorer = TopExp_Explorer(box, TopAbs_FACE)
    faces = []
    while explorer.More():
        faces.append(explorer.Current())
        explorer.Next()
    assert face_pick_ids(box, faces) == [1, 2, 3, 4, 5, 6]
