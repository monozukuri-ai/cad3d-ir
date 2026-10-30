from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest
from cad3d_ir import DirectoryResourceWriter, validate_package
from cad3d_ir.importers import Importer, ImportOptions
from cad3d_ir.mesh import validate_glb_bytes

from cad3d_ir_edrawings import (
    EdrawingsImporter,
    EdrawingsReadError,
    UnknownSelectionError,
    UnsupportedDocumentKindError,
    UnsupportedIRVersionError,
    sniff,
)
from cad3d_ir_edrawings.importer import limits_for, product_name
from tests.synthetic import (
    configurations_hsf,
    empty_hsf,
    foreign_zip,
    model_hsf,
    prefixed_container,
    tiny_jpeg,
    zip_container,
)

MESH = ImportOptions(include_visualization_mesh=True, include_brep=False)


def _json_chunk(payload: bytes) -> dict:
    length = struct.unpack_from("<I", payload, 12)[0]
    return json.loads(payload[20 : 20 + length].decode("utf-8"))


def _import(path: Path, output: Path, **kwargs):
    return EdrawingsImporter(**kwargs).import_file(
        path, options=MESH, resources=DirectoryResourceWriter(output)
    )


def test_importer_implements_core_protocol() -> None:
    assert isinstance(EdrawingsImporter(), Importer)


def test_signatures_and_probe_scores(part_path: Path, tmp_path: Path) -> None:
    assert sniff(model_hsf()[:64]) == ("raw_hsf", False)
    assert sniff(zip_container(model_hsf())[:64]) == ("zip", True)
    assert sniff(prefixed_container(zip_container(model_hsf()))[:200]) == (
        "prefixed_zip",
        True,
    )
    assert sniff(foreign_zip()[:64]) == ("zip", False)
    assert sniff(b"hello world" * 8) == (None, False)

    importer = EdrawingsImporter()
    assert importer.probe(part_path) == pytest.approx(0.9)
    renamed = tmp_path / "part.bin"
    renamed.write_bytes(part_path.read_bytes())
    assert importer.probe(renamed) == pytest.approx(0.5)
    container = tmp_path / "frame.easm"
    container.write_bytes(zip_container(model_hsf()))
    assert importer.probe(container) == pytest.approx(1.0)
    prefixed = tmp_path / "part.eprt"
    prefixed.write_bytes(prefixed_container(zip_container(model_hsf())))
    assert importer.probe(prefixed) == pytest.approx(1.0)
    office = tmp_path / "doc.eprt"
    office.write_bytes(foreign_zip())
    assert importer.probe(office) == pytest.approx(0.6)
    other = tmp_path / "notes.txt"
    other.write_text("hello\n", encoding="ascii")
    assert importer.probe(other) == 0.0
    assert importer.probe(tmp_path / "missing.eprt") == 0.0
    drawing = tmp_path / "sheet.edrw"
    drawing.write_bytes(model_hsf())
    assert importer.probe(drawing) == pytest.approx(0.5)
    assert EdrawingsImporter(accept_drawings=True).probe(drawing) == pytest.approx(0.9)


def test_part_import_groups_shared_bodies_into_products(
    part_path: Path, tmp_path: Path
) -> None:
    output = tmp_path / "out"
    result = _import(part_path, output)
    package = result.package
    validate_package(package, resource_root=output)

    assert package.units.length_unit == "mm"
    assert package.metadata["adapter"] == "cad3d-ir-edrawings"
    assert package.metadata["edrawings"]["scale_mm_per_unit"] == 1.0
    assert package.metadata["edrawings"]["placement"] == "estimated"
    [source] = package.source_documents
    assert source.format == "edrawings"
    assert source.properties["input_kind"] == "raw_hsf"
    assert source.properties["hsf_version"] == "19.10"
    assert source.properties["model_units"] == 1.0
    root, *parts = package.product_definitions
    assert root.kind == "part" and root.name == "Bracket-01"
    assert sorted(part.name for part in parts) == ["pin", "plate"]
    by_name = {part.name: part for part in parts}
    occurrences = {o.id: o for o in package.occurrences}
    root_configuration = next(
        c for c in package.configurations if c.product_id == root.id
    )
    assert root_configuration.name == "Default"
    assert len(root_configuration.occurrence_ids) == 3
    pins = [
        occurrences[i]
        for i in root_configuration.occurrence_ids
        if occurrences[i].product_id == by_name["pin"].id
    ]
    assert sorted(o.name for o in pins) == ["pin-1", "pin-2"]
    assert sorted(o.transform.matrix[0][3] for o in pins) == [10.0, 30.0]
    assert all(o.visible for o in package.occurrences)
    pin_configuration = next(
        c for c in package.configurations if c.product_id == by_name["pin"].id
    )
    assert len(pin_configuration.body_ids) == 1
    bodies = {b.id: b for b in package.bodies}
    [pin_body] = [bodies[i] for i in pin_configuration.body_ids]
    appearances = {a.id: a for a in package.appearances}
    assert appearances[pin_body.appearance_id].base_color == (0.0, 0.0, 1.0, 1.0)
    assert len(package.bodies) == 2

    [visualization] = package.visualizations
    assert visualization.bounds is not None
    assert visualization.bounds.min == pytest.approx((0.0, 0.0, 0.0))
    assert visualization.bounds.max == pytest.approx((31.0, 3.0, 4.0))
    assert visualization.statistics == {
        "triangles": 36,
        "mesh_triangles": 24,
        "line_segments": 36,
        "mesh_line_segments": 24,
        "vertices": 16,
        "nodes": 4,
        "meshes": 2,
        "placements": 3,
        "bodies": 2,
    }
    resource = next(r for r in package.resources if r.id == visualization.resource_id)
    payload = (output / resource.uri).read_bytes()
    report = validate_glb_bytes(payload)
    assert report.valid, report.errors
    document = _json_chunk(payload)
    nodes = {n["name"]: n for n in document["nodes"][1:]}
    assert set(nodes) == {"plate", "pin-1", "pin-2"}
    assert "matrix" not in nodes["plate"]
    assert nodes["pin-2"]["matrix"][12:15] == [30.0, 0.0, 0.0]
    assert nodes["pin-1"]["mesh"] == nodes["pin-2"]["mesh"]  # shared pin mesh
    assert nodes["pin-1"]["extras"]["id"] in occurrences
    assert {m["name"] for m in document["materials"]} == {"#ff0000", "#0000ff", "edges"}
    pin_mesh = document["meshes"][nodes["pin-1"]["mesh"]]
    assert pin_mesh["extras"]["bodies"] == [pin_body.id]
    assert [p["mode"] for p in pin_mesh["primitives"]] == [4, 1]
    assert pin_mesh["primitives"][0]["extras"] == {
        "entityKind": "face",
        "body": pin_body.id,
        "ranges": [[2, 0, 36]],
    }
    assert document["nodes"][0]["extras"]["cad3d_ir_edrawings"]["placement"] == (
        "estimated"
    )

    codes = {item.code for item in result.diagnostics}
    assert "EDRAWINGS_DISPLAY_MESH_ONLY" in codes
    assert "EDRAWINGS_NO_GEOMETRY" not in codes
    assert "EDRAWINGS_PLACEMENT_ESTIMATED" not in codes  # synthetic scene is complete
    assert result.statistics["bodies"] == 2 and result.statistics["instances"] == 3
    assert result.statistics["parts"] == 2 and result.statistics["occurrences"] == 3
    assert result.statistics["triangles"] == 36
    assert result.statistics["visualization_byte_length"] == len(payload)
    assert result.statistics["preview_byte_length"] == 0


def test_metres_are_assumed_without_model_units(tmp_path: Path) -> None:
    path = tmp_path / "metres.eprt"
    path.write_bytes(model_hsf(units=None))
    result = _import(path, tmp_path / "out")
    assert result.statistics["model_units"] is None
    assert result.statistics["scale_mm_per_unit"] == 1000.0
    [visualization] = result.package.visualizations
    assert visualization.bounds is not None
    assert visualization.bounds.max == pytest.approx((31000.0, 3000.0, 4000.0))
    pins = [o for o in result.package.occurrences if o.name.startswith("pin")]
    assert sorted(o.transform.matrix[0][3] for o in pins) == [10000.0, 30000.0]


def test_container_with_preview_is_an_assembly_with_preview_resource(
    assembly_path: Path, tmp_path: Path
) -> None:
    output = tmp_path / "out"
    result = _import(assembly_path, output)
    package = result.package
    validate_package(package, resource_root=output)
    assert package.product_definitions[0].kind == "assembly"
    assert package.source_documents[0].properties["input_kind"] == "zip"
    names = [
        entry["name"] for entry in package.source_documents[0].properties["entries"]
    ]
    assert names == ["eModel", "preview.jpg"]
    preview = next(r for r in package.resources if r.media_type == "image/jpeg")
    assert preview.uri.endswith(".jpg") and preview.properties["width"] == 4
    assert package.metadata["preview_resource_id"] == preview.id
    assert (output / preview.uri).read_bytes() == tiny_jpeg()
    assert result.statistics["preview_byte_length"] == len(tiny_jpeg())

    prefixed = tmp_path / "prefixed.easm"
    prefixed.write_bytes(prefixed_container(zip_container(model_hsf())))
    result = _import(prefixed, tmp_path / "prefixed")
    assert result.statistics["input_kind"] == "prefixed_zip"
    assert result.statistics["occurrences"] == 3


def test_hidden_components_are_skipped_or_kept_invisible(tmp_path: Path) -> None:
    path = tmp_path / "hidden.eprt"
    path.write_bytes(model_hsf(hidden_pin=True))
    result = _import(path, tmp_path / "a")
    assert result.statistics["instances"] == 3
    assert result.statistics["hidden_instances"] == 1
    assert len(result.package.occurrences) == 3
    skipped = next(
        d for d in result.diagnostics if d.code == "EDRAWINGS_HIDDEN_COMPONENTS_SKIPPED"
    )
    assert skipped.details["hidden_instances"] == 1

    result = _import(path, tmp_path / "b", include_hidden=True)
    occurrences = {o.name: o for o in result.package.occurrences}
    assert occurrences["pin-3"].visible is False
    assert occurrences["pin-3"].transform.matrix[0][3] == 50.0
    assert result.statistics["hidden_occurrences"] == 1
    assert result.package.visualizations[0].statistics["nodes"] == 4
    assert result.package.visualizations[0].bounds.max == pytest.approx(
        (31.0, 3.0, 4.0)
    )


def test_configurations_select_one_saved_group(tmp_path: Path) -> None:
    path = tmp_path / "configs.easm"
    path.write_bytes(configurations_hsf())
    result = _import(path, tmp_path / "first")
    package = result.package
    root_configuration = package.configurations[0]
    assert root_configuration.name == "Alpha"
    assert result.statistics["configurations"] == 2
    assert result.statistics["configuration"] == "Alpha"
    assert result.statistics["unplaced_bodies"] == 1
    codes = {item.code for item in result.diagnostics}
    assert {"EDRAWINGS_CONFIGURATION_SELECTED", "EDRAWINGS_UNPLACED_BODIES"} <= codes
    [body] = package.bodies
    appearance = next(a for a in package.appearances if a.id == body.appearance_id)
    assert appearance.base_color == (1.0, 0.0, 0.0, 1.0)

    beta = _import(path, tmp_path / "beta", configuration="Beta")
    assert beta.package.configurations[0].name == "Beta"
    [body] = beta.package.bodies
    appearance = next(a for a in beta.package.appearances if a.id == body.appearance_id)
    assert appearance.base_color == (0.0, 0.0, 1.0, 1.0)
    with pytest.raises(UnknownSelectionError, match="configuration_unknown"):
        _import(path, tmp_path / "gamma", configuration="Gamma")
    with pytest.raises(UnknownSelectionError, match="display_state_unknown"):
        _import(path, tmp_path / "state", display_state="disp9")


def test_documents_without_bodies(tmp_path: Path) -> None:
    empty = tmp_path / "empty.eprt"
    empty.write_bytes(empty_hsf())
    result = EdrawingsImporter().import_file(
        empty,
        options=ImportOptions(strict=False, include_visualization_mesh=True),
        resources=DirectoryResourceWriter(tmp_path / "a"),
    )
    assert result.package.bodies == [] and result.package.visualizations == []
    assert result.package.product_definitions[0].kind == "part"
    errors = [d for d in result.diagnostics if d.severity == "error"]
    assert [d.code for d in errors] == ["EDRAWINGS_NO_GEOMETRY"]
    with pytest.raises(EdrawingsReadError, match="no 3D bodies"):
        EdrawingsImporter().import_file(
            empty,
            options=ImportOptions(strict=True, include_visualization_mesh=True),
            resources=DirectoryResourceWriter(tmp_path / "b"),
        )

    preview_only = tmp_path / "preview.eprt"
    preview_only.write_bytes(zip_container(empty_hsf()))
    output = tmp_path / "c"
    result = EdrawingsImporter().import_file(
        preview_only,
        options=ImportOptions(strict=True, include_visualization_mesh=True),
        resources=DirectoryResourceWriter(output),
    )
    validate_package(result.package, resource_root=output)
    [warning] = [d for d in result.diagnostics if d.severity == "warning"]
    assert warning.code == "EDRAWINGS_PREVIEW_ONLY"
    [resource] = result.package.resources
    assert resource.media_type == "image/jpeg"
    assert not any(d.severity == "error" for d in result.diagnostics)


def test_drawings_are_rejected_unless_accepted(tmp_path: Path) -> None:
    drawing = tmp_path / "sheet.edrw"
    drawing.write_bytes(zip_container(empty_hsf()))
    with pytest.raises(UnsupportedDocumentKindError, match="accept_drawings"):
        _import(drawing, tmp_path / "a")
    result = _import(drawing, tmp_path / "b", accept_drawings=True)
    codes = {d.code for d in result.diagnostics}
    assert {"EDRAWINGS_DRAWING_SHEETS_IGNORED", "EDRAWINGS_PREVIEW_ONLY"} <= codes
    assert result.package.product_definitions[0].kind == "part"


def test_foreign_files_and_ir_versions_are_rejected(tmp_path: Path) -> None:
    junk = tmp_path / "junk.eprt"
    junk.write_bytes(b"hello world" * 64)
    with pytest.raises(EdrawingsReadError):
        _import(junk, tmp_path / "a")
    office = tmp_path / "office.eprt"
    office.write_bytes(foreign_zip())
    with pytest.raises(EdrawingsReadError):
        _import(office, tmp_path / "b")
    with pytest.raises(EdrawingsReadError, match="not a file"):
        _import(tmp_path / "missing.eprt", tmp_path / "c")
    with pytest.raises(UnsupportedIRVersionError):
        EdrawingsImporter().import_file(
            junk,
            options=ImportOptions(ir_version="9.9.9"),
            resources=DirectoryResourceWriter(tmp_path / "d"),
        )
    with pytest.raises(ValueError, match="limit profile"):
        EdrawingsImporter(limit_profile="huge")


def test_metadata_only_import_when_mesh_is_not_requested(
    part_path: Path, tmp_path: Path
) -> None:
    result = EdrawingsImporter().import_file(
        part_path,
        options=ImportOptions(include_visualization_mesh=False),
        resources=DirectoryResourceWriter(tmp_path / "out"),
    )
    package = result.package
    validate_package(package)
    assert package.visualizations == [] and package.resources == []
    assert len(package.occurrences) == 3 and len(package.bodies) == 2
    assert result.statistics["triangles"] == 0
    assert result.statistics["instances"] == 3


def test_product_names_and_limit_profiles() -> None:
    assert product_name(["SCR083-9", "SCR083-10"], "x") == "SCR083"
    assert product_name(["Pin", "Pin"], "x") == "Pin"
    assert product_name(["A-1", "B-2"], "x") == "A-1"
    assert product_name(["", ""], "fallback") == "fallback"
    assert product_name(["Body 0"], "box", prefer_fallback_for_generic=True) == "box"
    assert product_name(["Body 0"], "box") == "Body 0"
    assert product_name(["instance7"], "box", prefer_fallback_for_generic=True) == "box"
    service = limits_for("service")
    assert service.max_xml_elements == 2_000_000
    assert service.max_file_bytes == 256 << 20
    assert limits_for("desktop").max_hsf_vertices == 10_000_000
    with pytest.raises(ValueError, match="unknown limit profile"):
        limits_for("huge")
