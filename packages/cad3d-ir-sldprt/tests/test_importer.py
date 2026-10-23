from __future__ import annotations

import json
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest
import sldkit
from cad3d_ir import DirectoryResourceWriter, validate_package
from cad3d_ir.importers import Importer, ImportOptions
from cad3d_ir.mesh import validate_glb_bytes

from cad3d_ir_sldprt import (
    SldprtImporter,
    SldprtReadError,
    UnsupportedDocumentKindError,
    UnsupportedIRVersionError,
    solidworks_year_estimate,
)
from tests.synthetic import synthetic_document


def _json_chunk(payload: bytes) -> dict:
    length = struct.unpack_from("<I", payload, 12)[0]
    return json.loads(payload[20 : 20 + length].decode("utf-8"))


def test_importer_implements_core_protocol() -> None:
    assert isinstance(SldprtImporter(), Importer)


def test_probe_scores_synthetic_part_and_foreign_files(
    part_path: Path, tmp_path: Path
) -> None:
    importer = SldprtImporter()
    assert importer.probe(part_path) == pytest.approx(0.9)
    other = tmp_path / "notes.txt"
    other.write_text("hello\n", encoding="ascii")
    assert importer.probe(other) == 0.0
    assert importer.probe(tmp_path / "missing.SLDPRT") == 0.0
    renamed = tmp_path / "part.bin"
    renamed.write_bytes(part_path.read_bytes())
    assert importer.probe(renamed) == pytest.approx(0.5)


def test_import_writes_display_glb_with_shared_vertices(
    part_path: Path, tmp_path: Path
) -> None:
    output = tmp_path / "out"
    result = SldprtImporter().import_file(
        part_path,
        options=ImportOptions(include_visualization_mesh=True, include_brep=False),
        resources=DirectoryResourceWriter(output),
    )
    package = result.package
    validate_package(package, resource_root=output)

    assert package.units.length_unit == "mm"
    assert package.metadata["adapter"] == "cad3d-ir-sldprt"
    assert package.metadata["display_status"] == "decoded"
    [source] = package.source_documents
    assert source.format == "sldprt"
    assert source.properties["document_kind"] == "part"
    assert source.properties["configuration_names"] == ["Default"]
    assert source.properties["display_streams"][0]["part_count"] == 2
    [product] = package.product_definitions
    assert product.kind == "part" and product.name == "Bracket-01"
    [configuration] = package.configurations
    assert configuration.name == "Default"
    [body] = package.bodies
    assert configuration.body_ids == [body.id]
    [representation] = package.representations
    assert representation.kind == "mesh" and representation.role == "visualization"
    assert representation.derivation is not None
    assert representation.derivation.relation == "tessellated"
    assert representation.derivation.metadata["parts"] == 2
    [appearance] = package.appearances
    assert appearance.base_color == (1.0, 0.0, 0.0, 1.0)
    assert body.appearance_id == appearance.id
    [visualization] = package.visualizations
    assert visualization.statistics == {
        "triangles": 3,
        "line_segments": 7,
        "vertices": 7,
        "nodes": 2,
        "meshes": 1,
        "parts": 2,
    }
    assert visualization.bounds is not None
    assert visualization.bounds.max == pytest.approx((2.0, 2.0, 0.0))

    payload = (
        output / "geometry" / f"bracket-01-{source.sha256[:12]}.glb"
    ).read_bytes()
    report = validate_glb_bytes(payload)
    assert report.valid, report.errors
    document = _json_chunk(payload)
    materials = [material["name"] for material in document["materials"]]
    assert materials == ["red", "faces", "edges"]
    [mesh] = document["meshes"]
    assert mesh["extras"] == {"id": body.id, "product": product.id}
    primitives = mesh["primitives"]
    assert [primitive["mode"] for primitive in primitives] == [4, 4, 1]
    assert primitives[0]["extras"] == {"entityKind": "face", "ranges": [[1, 0, 6]]}
    assert primitives[1]["extras"] == {"entityKind": "face", "ranges": [[2, 0, 3]]}
    assert primitives[2]["extras"]["ranges"] == [[1, 0, 8], [2, 8, 6]]
    assert {primitive["attributes"]["POSITION"] for primitive in primitives} == {0}
    assert document["nodes"][1]["extras"] == {"id": product.id, "product": product.id}
    assert document["nodes"][0]["extras"]["cad3d_ir_sldprt"]["source_format"] == (
        "sldprt"
    )

    codes = {item.code for item in result.diagnostics}
    assert "SLDPRT_DISPLAY_MESH_ONLY" in codes
    assert "SLDPRT_COLORS_NOT_MAPPED" in codes
    assert "SLDPRT_NO_DISPLAY_DATA" not in codes
    assert result.statistics["triangles"] == 3
    assert result.statistics["display_parts"] == 2
    assert result.statistics["visualization_byte_length"] == len(payload)
    assert result.statistics["preview_byte_length"] == 0


def test_metadata_only_import_when_mesh_is_not_requested(
    part_path: Path, tmp_path: Path
) -> None:
    result = SldprtImporter().import_file(
        part_path,
        options=ImportOptions(include_visualization_mesh=False),
        resources=DirectoryResourceWriter(tmp_path / "out"),
    )
    package = result.package
    validate_package(package)
    assert package.visualizations == [] and package.resources == []
    [body] = package.bodies
    assert body.representation_ids == []
    assert package.appearances[0].base_color == (1.0, 0.0, 0.0, 1.0)
    assert result.statistics["triangles"] == 0
    assert result.statistics["display_parts"] == 2
    assert "SLDPRT_DISPLAY_MESH_ONLY" in {item.code for item in result.diagnostics}


def test_missing_display_data_is_reported_and_strict_raises(tmp_path: Path) -> None:
    path = tmp_path / "empty.SLDPRT"
    path.write_bytes(synthetic_document(display=False))
    options = ImportOptions(strict=False, include_visualization_mesh=True)
    result = SldprtImporter().import_file(
        path, options=options, resources=DirectoryResourceWriter(tmp_path / "a")
    )
    assert result.package.bodies == [] and result.package.visualizations == []
    [configuration] = result.package.configurations
    assert configuration.body_ids == []
    errors = [item for item in result.diagnostics if item.severity == "error"]
    assert [item.code for item in errors] == ["SLDPRT_NO_DISPLAY_DATA"]
    assert result.statistics["display_status"] == "unsupported"

    with pytest.raises(SldprtReadError, match="no display data"):
        SldprtImporter().import_file(
            path,
            options=ImportOptions(strict=True, include_visualization_mesh=True),
            resources=DirectoryResourceWriter(tmp_path / "b"),
        )


def test_derived_normals_are_reported(tmp_path: Path) -> None:
    path = tmp_path / "flat.SLDPRT"
    path.write_bytes(synthetic_document(normals=False))
    result = SldprtImporter().import_file(
        path,
        options=ImportOptions(include_visualization_mesh=True),
        resources=DirectoryResourceWriter(tmp_path / "out"),
    )
    by_code = {item.code: item for item in result.diagnostics}
    derived = by_code["SLDPRT_NORMALS_DERIVED"]
    assert derived.severity == "info" and derived.action == "approximated"
    assert derived.details is not None
    assert derived.details["sldkit_code"] == "display.normals_derived"


def test_assemblies_without_instance_records_stay_unplaced(tmp_path: Path) -> None:
    path = tmp_path / "frame.SLDASM"
    path.write_bytes(synthetic_document("ASSEMBLY"))
    with pytest.raises(UnsupportedDocumentKindError, match="accept_assemblies"):
        SldprtImporter(accept_assemblies=False).import_file(
            path,
            options=ImportOptions(),
            resources=DirectoryResourceWriter(tmp_path / "a"),
        )
    assert SldprtImporter(accept_assemblies=False).probe(path) == pytest.approx(0.5)
    assert SldprtImporter().probe(path) == pytest.approx(0.9)
    result = SldprtImporter().import_file(
        path,
        options=ImportOptions(include_visualization_mesh=True),
        resources=DirectoryResourceWriter(tmp_path / "b"),
    )
    [product] = result.package.product_definitions
    assert product.kind == "assembly"
    assert result.package.source_documents[0].format == "sldasm"
    codes = {item.code for item in result.diagnostics}
    # 部品レコードが無い: 表は根に置き、sldkit の未検証の注意もそのまま通す
    assert {
        "SLDPRT_ASSEMBLY_PLACEMENT_UNVERIFIED",
        "SLDASM_PLACEMENT_UNRESOLVED",
    } <= codes
    assert "SLDASM_PLACEMENT_FROM_COMPONENT_TRANSFORMS" not in codes
    assert (
        result.statistics["unplaced_parts"] == 2
        and result.statistics["occurrences"] == 0
    )
    assert result.package.visualizations[0].statistics["triangles"] == 3


def test_drawings_foreign_files_and_ir_versions_are_rejected(tmp_path: Path) -> None:
    drawing = tmp_path / "sheet.SLDDRW"
    drawing.write_bytes(synthetic_document("DRAWING"))
    with pytest.raises(UnsupportedDocumentKindError, match="drawings"):
        SldprtImporter().import_file(
            drawing,
            options=ImportOptions(),
            resources=DirectoryResourceWriter(tmp_path / "a"),
        )
    junk = tmp_path / "junk.SLDPRT"
    junk.write_bytes(b"hello world" * 64)
    with pytest.raises(SldprtReadError, match="not a readable SolidWorks document"):
        SldprtImporter().import_file(
            junk,
            options=ImportOptions(),
            resources=DirectoryResourceWriter(tmp_path / "b"),
        )
    with pytest.raises(SldprtReadError, match="not a file"):
        SldprtImporter().import_file(
            tmp_path / "missing.SLDPRT",
            options=ImportOptions(),
            resources=DirectoryResourceWriter(tmp_path / "c"),
        )
    with pytest.raises(UnsupportedIRVersionError):
        SldprtImporter().import_file(
            junk,
            options=ImportOptions(ir_version="9.9.9"),
            resources=DirectoryResourceWriter(tmp_path / "d"),
        )


def test_preview_image_and_properties_become_package_data(
    part_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    png = (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">IIBBBBB", 2, 3, 8, 2, 0, 0, 0)
        + bytes(4)
        + bytes(12)
    )
    resource = SimpleNamespace(kind=sldkit.BinaryResourceKind.PREVIEW_PNG)
    document = SimpleNamespace(
        document_kind=SimpleNamespace(value=sldkit.DocumentKind.PART),
        envelope=SimpleNamespace(value=sldkit.Envelope.MODERN_CHUNK),
        internal_version=SimpleNamespace(value=18000),
        configurations=[
            SimpleNamespace(name=SimpleNamespace(value="Machined"), preview=None),
            SimpleNamespace(name=SimpleNamespace(value="Cast"), preview=resource),
        ],
        properties=[
            SimpleNamespace(
                name=SimpleNamespace(value="PartNo"),
                raw_value=SimpleNamespace(value="A-100"),
                value_state=sldkit.PropertyValueState.PRESENT,
            ),
            SimpleNamespace(
                name=SimpleNamespace(value="Empty"),
                raw_value=None,
                value_state=sldkit.PropertyValueState.EMPTY,
            ),
            SimpleNamespace(
                name=SimpleNamespace(value="PartNo"),
                raw_value=SimpleNamespace(value="duplicate"),
                value_state=sldkit.PropertyValueState.PRESENT,
            ),
        ],
        preview=None,
        units=SimpleNamespace(
            length=SimpleNamespace(name=SimpleNamespace(value="inch"))
        ),
    )
    monkeypatch.setattr(
        sldkit,
        "parse_file",
        lambda path, profile: SimpleNamespace(document=document),
    )
    monkeypatch.setattr(
        sldkit,
        "extract_resource_file",
        lambda path, res, profile: SimpleNamespace(data=png),
    )
    output = tmp_path / "out"
    result = SldprtImporter().import_file(
        part_path,
        options=ImportOptions(include_visualization_mesh=True),
        resources=DirectoryResourceWriter(output),
    )
    package = result.package
    validate_package(package, resource_root=output)
    [source] = package.source_documents
    assert source.version == "SOLIDWORKS 2025"
    assert source.properties["internal_version"] == 18000
    assert source.properties["configuration_names"] == ["Machined", "Cast"]
    assert source.properties["custom_properties"] == {"PartNo": "A-100"}
    assert source.properties["document_length_unit"] == "inch"
    assert package.configurations[0].name == "Machined"
    preview = next(r for r in package.resources if r.media_type == "image/png")
    assert preview.uri.startswith("preview/") and preview.uri.endswith(".png")
    assert preview.properties["width"] == 2 and preview.properties["height"] == 3
    assert package.metadata["preview_resource_id"] == preview.id
    assert (output / preview.uri).read_bytes() == png
    assert result.statistics["preview_byte_length"] == len(png)
    assert result.statistics["custom_properties"] == 1


def test_preview_only_document_keeps_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "legacy.SLDPRT"
    path.write_bytes(synthetic_document(display=False))
    dib = struct.pack("<IiiHHIIiiII", 40, 2, 2, 1, 24, 0, 0, 0, 0, 0, 0) + bytes(16)
    document = SimpleNamespace(
        document_kind=SimpleNamespace(value=sldkit.DocumentKind.PART),
        envelope=SimpleNamespace(value=sldkit.Envelope.OLE2_CFB),
        internal_version=SimpleNamespace(value=2200),
        configurations=[],
        properties=[],
        preview=SimpleNamespace(kind=sldkit.BinaryResourceKind.PREVIEW_DIB),
        units=None,
    )
    monkeypatch.setattr(
        sldkit, "parse_file", lambda path, profile: SimpleNamespace(document=document)
    )
    monkeypatch.setattr(
        sldkit,
        "extract_resource_file",
        lambda path, res, profile: SimpleNamespace(data=dib),
    )
    output = tmp_path / "out"
    result = SldprtImporter().import_file(
        path,
        options=ImportOptions(strict=True, include_visualization_mesh=True),
        resources=DirectoryResourceWriter(output),
    )
    package = result.package
    validate_package(package, resource_root=output)
    assert package.bodies == [] and package.visualizations == []
    [resource] = package.resources
    assert resource.media_type == "image/bmp" and resource.encoding == "bmp"
    assert (output / resource.uri).read_bytes()[:2] == b"BM"
    [warning] = [item for item in result.diagnostics if item.severity == "warning"]
    assert warning.code == "SLDPRT_PREVIEW_ONLY"
    assert warning.details == {
        "envelope": "modern_chunk",
        "display_status": "unsupported",
        "legacy": False,
    }
    assert package.source_documents[0].version == "SOLIDWORKS 2009"


def test_year_estimate() -> None:
    assert solidworks_year_estimate(8000) == 2015
    assert solidworks_year_estimate(19000) == 2026
    assert solidworks_year_estimate(7000) == 2014
    assert solidworks_year_estimate(None) is None
    assert solidworks_year_estimate(5) is None
