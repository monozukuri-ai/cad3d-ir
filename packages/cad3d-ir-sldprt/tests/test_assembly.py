from __future__ import annotations

import json
import struct
from pathlib import Path

from cad3d_ir import DirectoryResourceWriter, validate_package
from cad3d_ir.importers import ImportOptions
from cad3d_ir.mesh import validate_glb_bytes

from cad3d_ir_sldprt import SldprtImporter
from cad3d_ir_sldprt.assembly import (
    assembly_name,
    instance_records,
    mfc_strings,
    transform_from_tokens,
)
from tests.synthetic import QUAD, TRIANGLE, assembly_document, mfc_string

# 90 度 Z 回転 + 平行移動 (0.01, 0.02, 0.003) m、行ベクトル規約の 16 トークン
ROTATED = "0 1 0 0 -1 0 0 0 0 0 1 0 0.01 0.02 0.003 1"


def _json_chunk(payload: bytes) -> dict:
    length = struct.unpack_from("<I", payload, 12)[0]
    return json.loads(payload[20 : 20 + length].decode("utf-8"))


def test_transform_tokens_become_a_column_vector_matrix_in_millimetres() -> None:
    matrix = transform_from_tokens(ROTATED.split())
    assert matrix is not None
    assert matrix[0] == (0.0, -1.0, 0.0, 10.0)
    assert matrix[1] == (1.0, 0.0, 0.0, 20.0)
    assert matrix[2] == (0.0, 0.0, 1.0, 3.0)
    assert matrix[3] == (0.0, 0.0, 0.0, 1.0)
    assert transform_from_tokens(["1"] * 15) is None
    assert transform_from_tokens(["x"] * 16) is None
    assert transform_from_tokens(None) is None


def test_instance_records_follow_the_stream_strings() -> None:
    data = (
        b"\x00\x01"
        + b"uoTempAssemblySHDData_c"
        + mfc_string("Top")
        + b"junk"
        + mfc_string("Plate-1@Top")
        + bytes(8)
        + mfc_string("Bolt-2@Sub-1@Top")
        + mfc_string("Other-3@Elsewhere")
        + mfc_string("no instance")
    )
    assert [text for _, text in mfc_strings(data)][:2] == ["Top", "Plate-1@Top"]
    assert assembly_name(data) == "Top"
    records = instance_records(data, "Top")
    assert [(r.name, r.number, r.nested) for r in records] == [
        ("Plate", 1, False),
        ("Bolt", 2, True),
    ]
    assert records[0].offset < records[1].offset


def test_assembly_instances_are_placed_with_their_transforms(tmp_path: Path) -> None:
    path = tmp_path / "Frame.SLDASM"
    path.write_bytes(
        assembly_document(
            [
                {"name": "Plate", "number": 1, "points": QUAD},
                {"name": "Pin", "number": 1, "transform": ROTATED, "points": TRIANGLE},
                {"name": "Lid", "number": 1, "hidden": True, "points": QUAD},
                {"name": "Spare", "number": 2, "suppressed": True, "points": None},
            ]
        )
    )
    output = tmp_path / "out"
    result = SldprtImporter().import_file(
        path,
        options=ImportOptions(include_visualization_mesh=True, include_brep=False),
        resources=DirectoryResourceWriter(output),
    )
    package = result.package
    validate_package(package, resource_root=output)

    assert package.metadata["assembly_name"] == "Frame"
    assert package.metadata["placed_instances"] == 3
    products = {p.name: p for p in package.product_definitions}
    assert products["Frame"].kind == "assembly"
    assert {"Plate", "Pin", "Lid"} <= set(products) and "Spare" not in products
    occurrences = {o.name: o for o in package.occurrences}
    assert set(occurrences) == {"Plate-1", "Pin-1", "Lid-1"}
    assert occurrences["Pin-1"].transform.matrix[0][3] == 10.0
    assert occurrences["Pin-1"].transform.matrix[1][0] == 1.0
    assert occurrences["Lid-1"].visible is False
    [root_configuration] = [
        c for c in package.configurations if c.product_id == products["Frame"].id
    ]
    assert set(root_configuration.occurrence_ids) == {o.id for o in package.occurrences}
    assert root_configuration.body_ids == []  # 根に残る表は無い
    body_names = {b.name for b in package.bodies}
    assert body_names == {"Plate-1 body", "Pin-1 body"}

    [visualization] = package.visualizations
    assert visualization.statistics["nodes"] == 3  # 根 + Plate + Pin(Lid は非表示)
    assert visualization.statistics["triangles"] == 3
    resource = next(r for r in package.resources if r.id == visualization.resource_id)
    payload = (output / resource.uri).read_bytes()
    report = validate_glb_bytes(payload)
    assert report.valid, report.errors
    document = _json_chunk(payload)
    nodes = {n["name"]: n for n in document["nodes"]}
    assert "Pin-1" in nodes and "matrix" in nodes["Pin-1"]
    pin = nodes["Pin-1"]["matrix"]
    assert pin[0:3] == [0.0, 1.0, 0.0] and pin[12:15] == [10.0, 20.0, 3.0]
    assert "matrix" not in nodes["Plate-1"]
    assert nodes["Pin-1"]["extras"]["id"] == occurrences["Pin-1"].id
    primitives = document["meshes"][1]["primitives"]
    assert primitives[0]["extras"]["ranges"] == [[2, 0, 3]]  # pick id は表の通し番号

    codes = {item.code for item in result.diagnostics}
    assert "SLDASM_PLACEMENT_FROM_COMPONENT_TRANSFORMS" in codes
    assert "SLDASM_COMPONENTS_SKIPPED" in codes
    assert "SLDPRT_ASSEMBLY_PLACEMENT_UNVERIFIED" not in codes
    assert "SLDASM_PLACEMENT_UNRESOLVED" not in codes
    assert result.statistics["occurrences"] == 3 and result.statistics["parts"] == 3
    assert result.statistics["hidden_instances"] == 1
    assert result.statistics["unplaced_parts"] == 0


def test_tables_without_a_record_stay_at_the_root(tmp_path: Path) -> None:
    path = tmp_path / "Frame.SLDASM"
    path.write_bytes(
        assembly_document(
            [{"name": "Pin", "number": 1, "transform": ROTATED, "points": TRIANGLE}],
            leading_points=QUAD,
        )
    )
    output = tmp_path / "out"
    result = SldprtImporter().import_file(
        path,
        options=ImportOptions(include_visualization_mesh=True),
        resources=DirectoryResourceWriter(output),
    )
    package = result.package
    validate_package(package, resource_root=output)
    root = package.product_definitions[0]
    [root_configuration] = [
        c for c in package.configurations if c.product_id == root.id
    ]
    assert len(root_configuration.body_ids) == 1
    assert len(package.occurrences) == 1
    unresolved = next(
        d for d in result.diagnostics if d.code == "SLDASM_PLACEMENT_UNRESOLVED"
    )
    assert unresolved.details == {
        "tables": 1,
        "nested_records": 0,
        "unassigned_tables": 1,
    }
    document = _json_chunk(
        (
            output / next(r.uri for r in package.resources if r.uri.endswith(".glb"))
        ).read_bytes()
    )
    assert {n["name"] for n in document["nodes"]} == {
        "Frame",
        "Frame (unplaced)",
        "Pin-1",
    }
    assert result.statistics["unplaced_parts"] == 1
