from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest
from cad3d_ir import DirectoryResourceWriter, validate_package
from cad3d_ir.importers import Importer, ImportOptions
from cad3d_ir.mesh import read_glb, validate_glb_bytes

from cad3d_ir_icad import (
    IcadImporter,
    IcadReadError,
    UnsupportedIRVersionError,
    is_icd_signature,
    read_source,
    version_label,
)

# The authored box payload spans 10 x 20 x 30 m; in millimetres its extent is
# 10,000 x 20,000 x 30,000 and the child part sits at (100, 0, 0) with X along +Y.
BOX_EXTENT = (10_000.0, 20_000.0, 30_000.0)


def _json_chunk(payload: bytes) -> dict:
    length = struct.unpack_from("<I", payload, 12)[0]
    return json.loads(payload[20 : 20 + length].decode("utf-8"))


def _import(path: Path, output: Path, **overrides):
    options = ImportOptions(
        include_visualization_mesh=True, include_brep=False, strict=False
    )
    if overrides:
        options = ImportOptions(
            **{**{f: getattr(options, f) for f in options.__slots__}, **overrides}
        )
    return IcadImporter().import_file(
        path, options=options, resources=DirectoryResourceWriter(output)
    )


def test_importer_implements_core_protocol() -> None:
    assert isinstance(IcadImporter(), Importer)
    assert IcadImporter().format_name == "icd"


def test_probe_uses_the_header_signature(box_path: Path, tmp_path: Path) -> None:
    importer = IcadImporter()
    assert importer.probe(box_path) == pytest.approx(0.9)
    renamed = tmp_path / "model.bin"
    renamed.write_bytes(box_path.read_bytes())
    assert importer.probe(renamed) == pytest.approx(0.6)
    other = tmp_path / "notes.icd"
    other.write_text("<?xml version='1.0'?><SCL/>", encoding="ascii")
    assert importer.probe(other) == 0.0
    assert importer.probe(tmp_path / "missing.icd") == 0.0
    assert is_icd_signature(box_path.read_bytes()[:256])
    assert not is_icd_signature(b"MOD0" + b"\0" * 300)


def test_version_label_reads_the_header_bytes(box_path: Path, v8l5_path: Path) -> None:
    assert version_label(b"\0\x08\0\x03") == "V8L3"
    assert version_label(b"\0\x01\0\x09") == "V1L9"
    assert version_label(b"\x01\0\0\x03") is None
    assert read_source(box_path, read_shapes=False).version_label == "V7L7"
    assert read_source(v8l5_path, read_shapes=False).version_label == "V8L5"


def test_box_imports_as_a_two_product_tree_with_one_shared_mesh(
    box_path: Path, tmp_path: Path
) -> None:
    output = tmp_path / "out"
    result = _import(box_path, output)
    package = result.package
    validate_package(package, resource_root=output)
    assert [p.name for p in package.product_definitions] == ["部品", "部品"]
    assert [p.kind for p in package.product_definitions] == ["assembly", "part"]
    assert len(package.occurrences) == 1 and len(package.bodies) == 1
    assert len(package.roots) == 1
    root_configuration = next(
        c for c in package.configurations if c.product_id == package.roots[0].product_id
    )
    assert root_configuration.occurrence_ids == [package.occurrences[0].id]
    occurrence = package.occurrences[0]
    assert occurrence.transform.matrix[0][3] == pytest.approx(100.0)
    (visualization,) = package.visualizations
    resource = next(r for r in package.resources if r.id == visualization.resource_id)
    payload = (output / resource.uri).read_bytes()
    assert validate_glb_bytes(payload).valid
    scene = read_glb(payload)
    assert scene.placed_triangle_count == 12
    assert scene.placed_line_segment_count == 12
    gltf = _json_chunk(payload)
    assert [node["name"] for node in gltf["nodes"][1:]] == ["部品", "部品"]
    assert len(gltf["meshes"]) == 1
    assert visualization.statistics["triangles"] == 12
    assert visualization.statistics["nodes"] == 3
    bounds = visualization.bounds
    assert bounds is not None
    for axis, size in enumerate(BOX_EXTENT):
        assert bounds.max[axis] - bounds.min[axis] == pytest.approx(size, abs=1e-3)
    codes = [d.code for d in result.diagnostics]
    assert "ICD_SAVED_BREP" in codes and "ICD_APPEARANCE_NOT_MAPPED" in codes
    assert "ICD_NO_GEOMETRY" not in codes and "ICD_BODY_UNSUPPORTED" not in codes
    statistics = result.statistics
    assert statistics["icd_version"] == "V7L7"
    assert statistics["parts"] == 2 and statistics["parts_converted"] == 1
    assert statistics["parts_with_bodies"] == 1 and statistics["unique_meshes"] == 1
    assert statistics["bodies_by_source"] == {"saved": 1}
    assert statistics["triangles"] == 12 and statistics["external_parts"] == 0
    assert package.source_documents[0].version == "iCAD SX V7L7"
    assert package.metadata["adapter"] == "cad3d-ir-icad"


def test_brep_resources_are_written_on_request(box_path: Path, tmp_path: Path) -> None:
    output = tmp_path / "out"
    result = _import(box_path, output, include_brep=True)
    validate_package(result.package, resource_root=output)
    (representation,) = result.package.representations
    assert representation.kind == "brep" and representation.fidelity == "exact"
    assert result.package.bodies[0].representation_ids == [representation.id]
    resource = next(
        r for r in result.package.resources if r.id == representation.resource_id
    )
    assert resource.uri.endswith(".brep")
    assert (output / resource.uri).read_bytes().startswith(b"DBRep_DrawableShape")


def test_external_reference_is_reported_and_kept_as_a_dependency(
    external_path: Path, tmp_path: Path
) -> None:
    result = _import(external_path, tmp_path / "out")
    package = result.package
    assert not package.visualizations and not package.bodies
    codes = {d.code for d in result.diagnostics}
    assert "ICD_EXTERNAL_PART_MISSING" in codes and "ICD_NO_GEOMETRY" in codes
    severity = next(
        d.severity for d in result.diagnostics if d.code == "ICD_NO_GEOMETRY"
    )
    assert severity == "warning"
    (dependency,) = package.source_documents[0].dependencies
    assert dependency.uri == "OTHER-PART" and dependency.kind == "component"
    external = package.product_definitions[1]
    assert external.properties["external_reference"] == "OTHER-PART"
    assert result.statistics["external_parts"] == 1
    with pytest.raises(IcadReadError, match="external"):
        _import(external_path, tmp_path / "strict", strict=True)


def test_unqualified_version_fails_closed(v8l5_path: Path, tmp_path: Path) -> None:
    result = _import(v8l5_path, tmp_path / "out")
    assert not result.package.visualizations
    (diagnostic,) = [
        d for d in result.diagnostics if d.code == "ICD_VERSION_UNQUALIFIED"
    ]
    assert diagnostic.severity == "error" and diagnostic.details == {
        "version_label": "V8L5",
        "byte_order": "little",
    }
    assert result.statistics["version_unqualified"] is True
    assert result.statistics["icd_version"] == "V8L5"
    with pytest.raises(IcadReadError, match="unqualified"):
        _import(v8l5_path, tmp_path / "strict", strict=True)


def test_two_d_document_has_views_but_no_solids(
    sheet_path: Path, tmp_path: Path
) -> None:
    result = _import(sheet_path, tmp_path / "out")
    assert not result.package.visualizations
    codes = [d.code for d in result.diagnostics]
    assert "ICD_2D_VIEWS_PRESENT" in codes and "ICD_NO_GEOMETRY" in codes
    assert result.statistics["views_2d"] == 2
    assert result.statistics["entities_2d"] == 0
    assert result.statistics["parts"] == 0


def test_rejects_other_ir_versions_and_missing_files(
    box_path: Path, tmp_path: Path
) -> None:
    with pytest.raises(UnsupportedIRVersionError):
        IcadImporter().import_file(
            box_path,
            options=ImportOptions(ir_version="0.0.1"),
            resources=DirectoryResourceWriter(tmp_path / "out"),
        )
    with pytest.raises(IcadReadError):
        IcadImporter().import_file(
            tmp_path / "missing.icd",
            options=ImportOptions(),
            resources=DirectoryResourceWriter(tmp_path / "out"),
        )
    other = tmp_path / "other.icd"
    other.write_bytes(b"not an icd file" * 20)
    with pytest.raises(IcadReadError):
        IcadImporter().import_file(
            other,
            options=ImportOptions(),
            resources=DirectoryResourceWriter(tmp_path / "out2"),
        )
