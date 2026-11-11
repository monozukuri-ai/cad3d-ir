from __future__ import annotations

import json
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest
from cad3d_ir import DirectoryResourceWriter, validate_package
from cad3d_ir.importers import Importer, ImportOptions
from cad3d_ir.mesh import validate_glb_bytes

from cad3d_ir_inventor import (
    InventorImporter,
    InventorReadError,
    UnsupportedIRVersionError,
    classify,
    is_compound_file,
    map_source,
    read_source,
    release_of_major,
)

from .conftest import PNG, converted_body, needs_kit, rejected_body, stand_in_source

OPTIONS = ImportOptions(
    include_visualization_mesh=True, include_brep=False, strict=False
)


def _json_chunk(payload: bytes) -> dict:
    length = struct.unpack_from("<I", payload, 12)[0]
    return json.loads(payload[20 : 20 + length].decode("utf-8"))


def _codes(result) -> set[str]:
    return {item.code for item in result.diagnostics}


def _map(source, output: Path, options: ImportOptions = OPTIONS):
    return map_source(
        source, options=options, resources=DirectoryResourceWriter(output)
    )


def _visualization_payload(result, output: Path) -> bytes:
    (visualization,) = result.package.visualizations
    resource = next(
        r for r in result.package.resources if r.id == visualization.resource_id
    )
    return (output / resource.uri).read_bytes()


def test_importer_implements_core_protocol() -> None:
    assert isinstance(InventorImporter(), Importer)
    assert InventorImporter().format_name == "ipt"
    assert InventorImporter().suffixes == {".ipt"}
    assert release_of_major(25) == "Inventor 2021"
    assert release_of_major(None) is None


def test_probe_uses_the_compound_header_and_the_suffix(
    box_path: Path, tmp_path: Path
) -> None:
    importer = InventorImporter()
    assert importer.probe(box_path) == pytest.approx(0.9)
    renamed = tmp_path / "model.bin"
    renamed.write_bytes(box_path.read_bytes())
    assert importer.probe(renamed) == pytest.approx(0.2)
    other = tmp_path / "notes.ipt"
    other.write_text("ISO-10303-21;\nHEADER;\n", encoding="ascii")
    assert importer.probe(other) == 0.0
    assert importer.probe(tmp_path / "missing.ipt") == 0.0
    assert is_compound_file(box_path.read_bytes()[:8])
    assert not is_compound_file(b"ISO-10303-21;")


@needs_kit
def test_box_becomes_one_body_with_a_glb(box_path: Path, tmp_path: Path) -> None:
    output = tmp_path / "package"
    result = InventorImporter().import_file(
        box_path, options=OPTIONS, resources=DirectoryResourceWriter(output)
    )
    validate_package(result.package, resource_root=output)
    assert _codes(result) == {
        "INVENTOR_EXACT_BREP",
        "INVENTOR_STATE_UNVERIFIED",
        "INVENTOR_APPEARANCE_NOT_READ",
    }
    statistics = result.statistics
    assert statistics["major"] == 31 and statistics["release"] == "Inventor 2027"
    assert statistics["save_version"] == 23200
    assert statistics["candidate_profile"] is False
    assert statistics["state_verified"] is False
    assert statistics["bodies"] == 1 and statistics["bodies_converted"] == 1
    assert statistics["bodies_open_shell"] == statistics["bodies_unsupported"] == 0
    assert statistics["faces"] == 6 and statistics["triangles"] == 12
    assert statistics["approximated_curves"] == 0

    (document,) = result.package.source_documents
    assert document.format == "ipt" and document.version == "Inventor 2027"
    assert document.properties["iproperties"] == {
        "title": "Box 10 x 20 x 30",
        "part_number": "BOX-001",
    }
    assert document.properties["units"] == "mm"
    (product,) = result.package.product_definitions
    assert product.kind == "part" and product.properties["part_number"] == "BOX-001"
    (configuration,) = result.package.configurations
    assert configuration.name == "Stored state"
    (body,) = result.package.bodies
    assert body.name == "Body_1" and configuration.body_ids == [body.id]
    assert body.properties["volume_mm3"] == pytest.approx(6000.0)
    assert result.package.occurrences == []

    (visualization,) = result.package.visualizations
    assert visualization.bounds is not None
    extent = [
        high - low
        for low, high in zip(
            visualization.bounds.min, visualization.bounds.max, strict=True
        )
    ]
    assert extent == pytest.approx([10.0, 20.0, 30.0], abs=1e-6)
    payload = _visualization_payload(result, output)
    assert validate_glb_bytes(payload).valid
    gltf = _json_chunk(payload)
    (node,) = [n for n in gltf["nodes"] if "mesh" in n]
    assert node["name"] == "Body_1" and node["extras"]["body"] == body.id
    assert "cad3d_ir_inventor" in json.dumps(gltf)


@needs_kit
def test_ring_reads_the_2026_profile_and_writes_a_brep(
    ring_path: Path, tmp_path: Path
) -> None:
    output = tmp_path / "package"
    result = InventorImporter().import_file(
        ring_path,
        options=ImportOptions(
            include_visualization_mesh=False, include_brep=True, strict=True
        ),
        resources=DirectoryResourceWriter(output),
    )
    validate_package(result.package, resource_root=output)
    assert result.statistics["major"] == 30
    assert result.statistics["release"] == "Inventor 2026"
    assert result.package.visualizations == []
    (representation,) = result.package.representations
    assert representation.kind == "brep" and representation.fidelity == "exact"
    (resource,) = result.package.resources
    assert (output / resource.uri).read_bytes().startswith(b"DBRep_DrawableShape")
    (body,) = result.package.bodies
    assert body.representation_ids == [representation.id]
    # pi * (10^2 - 4^2) * 20
    assert body.properties["volume_mm3"] == pytest.approx(5277.8757, rel=1e-6)


@needs_kit
def test_read_source_feeds_classification_and_times(box_path: Path) -> None:
    source = read_source(box_path)
    assert source.kind == "part" and source.major == 31
    assert source.profile_status == "supported_stored_table"
    assert not source.candidate_profile and not source.profile_unsupported
    assert source.thumbnail is None and source.geometry_reasons == []
    (body,) = classify(source).bodies
    assert body.status == "converted" and body.exact and body.label == "Body_1"
    assert body.bounds is not None
    unconverted = read_source(box_path, convert=False)
    assert unconverted.conversion is None and classify(unconverted).bodies == []


@needs_kit
def test_unreadable_sources(tmp_path: Path, box_path: Path) -> None:
    text = tmp_path / "text.ipt"
    text.write_text("not a compound file", encoding="ascii")
    with pytest.raises(InventorReadError, match="compound file"):
        read_source(text)
    with pytest.raises(InventorReadError, match="not a file"):
        read_source(tmp_path / "missing.ipt")
    # A compound-file header followed by nothing inventor-kit can read.
    damaged = tmp_path / "damaged.ipt"
    damaged.write_bytes(box_path.read_bytes()[:8] + b"\0" * 2048)
    source = read_source(damaged)
    assert source.read_error is not None and source.document is None
    result = _map(source, tmp_path / "damaged")
    assert _codes(result) == {"INVENTOR_READ_FAILED"}
    assert result.statistics["read_failed"] is True
    assert result.package.bodies == [] and result.package.visualizations == []
    with pytest.raises(InventorReadError, match="unreadable document"):
        _map(
            source,
            tmp_path / "strict",
            ImportOptions(include_visualization_mesh=True, strict=True),
        )


@needs_kit
def test_parser_limits_are_reported(box_path: Path, tmp_path: Path) -> None:
    import inventor_kit

    source = read_source(box_path, limits=inventor_kit.Limits(max_file_bytes=1024))
    assert source.read_error is not None and source.read_error.limit
    assert _codes(_map(source, tmp_path / "limit")) == {"INVENTOR_LIMIT"}


def test_unsupported_ir_version_is_refused(box_path: Path, tmp_path: Path) -> None:
    with pytest.raises(UnsupportedIRVersionError):
        InventorImporter().import_file(
            box_path,
            options=ImportOptions(ir_version="0.0.1"),
            resources=DirectoryResourceWriter(tmp_path / "out"),
        )


def test_bodies_are_sorted_by_outcome(tmp_path: Path) -> None:
    fit = {
        "type": "intcurve-curve",
        "subtype": "int_int_cur",
        "fit_tolerance_mm": 0.004,
    }
    surface_fit = {
        "type": "spline-surface",
        "subtype": "rb_blend_spl_sur",
        "fit_tolerance_mm": 0.01,
    }
    source = stand_in_source(
        tmp_path,
        bodies=(
            converted_body(1),
            converted_body(2, approximations=(fit, fit, surface_fit)),
            rejected_body(
                3,
                "unsupported",
                "geometry.surface_unsupported",
                "face $29 surface: unsupported geometry spline-surface",
                "helix_spl_circ",
            ),
            rejected_body(
                4,
                "unsupported",
                "geometry.sewing_no_shell",
                "shell $8: sewing produced no shell",
            ),
            rejected_body(5, "open_shell", "geometry.shell_open", "shell $9 is open"),
            rejected_body(
                6, "failed", "geometry.face_invalid", "face $395: invalid face"
            ),
        ),
    )
    classification = classify(source)
    assert [body.status for body in classification.bodies] == [
        "converted",
        "converted",
        "unsupported",
        "sheet",
        "sheet",
        "failed",
    ]
    assert classification.unsupported_reasons == {
        "helix_spl_circ": 1,
        "geometry.face_invalid": 1,
    }
    output = tmp_path / "package"
    result = _map(source, output)
    validate_package(result.package, resource_root=output)
    assert _codes(result) == {
        "INVENTOR_EXACT_BREP",
        "INVENTOR_STATE_UNVERIFIED",
        "INVENTOR_APPEARANCE_NOT_READ",
        "INVENTOR_APPROXIMATED_GEOMETRY",
        "INVENTOR_BODY_UNSUPPORTED",
        "INVENTOR_BODY_SHEET",
        "INVENTOR_BODY_FAILED",
    }
    by_code = {item.code: item for item in result.diagnostics}
    assert by_code["INVENTOR_BODY_UNSUPPORTED"].details["reasons"] == {
        "helix_spl_circ": 1
    }
    assert by_code["INVENTOR_BODY_SHEET"].details["bodies"] == [4, 5]
    approximated = by_code["INVENTOR_APPROXIMATED_GEOMETRY"].details
    assert approximated["curves"] == 2 and approximated["surfaces"] == 1
    assert approximated["largest_fit_tolerance_mm"] == pytest.approx(0.01)
    statistics = result.statistics
    assert statistics["bodies"] == 6 and statistics["bodies_converted"] == 2
    assert statistics["bodies_open_shell"] == 2
    assert statistics["bodies_unsupported"] == 1 and statistics["bodies_failed"] == 1
    assert statistics["approximated_curves"] == 2
    assert statistics["approximated_surfaces"] == 1
    assert [body.name for body in result.package.bodies] == ["Body_1", "Body_2"]
    gltf = _json_chunk(_visualization_payload(result, output))
    assert sorted(n["name"] for n in gltf["nodes"] if "mesh" in n) == [
        "Body_1",
        "Body_2",
    ]
    # A body on stored fits is a derived, not an exact, B-Rep.
    brep = _map(
        source,
        tmp_path / "brep",
        ImportOptions(include_visualization_mesh=False, include_brep=True),
    )
    assert [r.fidelity for r in brep.package.representations] == [
        "exact",
        "approximate",
    ]


def test_candidate_generation_is_read_with_a_warning(tmp_path: Path) -> None:
    source = stand_in_source(
        tmp_path,
        bodies=(converted_body(1),),
        major=22,
        profile_status="candidate_stored_table",
        candidate_codes=("candidate.profile_candidate",),
    )
    assert source.candidate_profile and source.release == "Inventor 2018"
    result = _map(source, tmp_path / "package")
    (warning,) = [d for d in result.diagnostics if d.severity == "warning"]
    assert warning.code == "INVENTOR_PROFILE_CANDIDATE"
    assert warning.details["segment_major"] == 22
    assert result.statistics["candidate_profile"] is True
    assert result.statistics["bodies_converted"] == 1


def test_unread_generation_fails_closed_and_keeps_the_preview(tmp_path: Path) -> None:
    thumbnail = SimpleNamespace(mime_type="image/png", data=PNG, width=1, height=1)
    source = stand_in_source(
        tmp_path,
        bodies=None,
        major=14,
        profile_status="unsupported",
        candidate_codes=("candidate.profile_unsupported",),
        reasons=("segment major 14 is not supported",),
        thumbnails=(thumbnail,),
    )
    assert source.profile_unsupported
    output = tmp_path / "package"
    result = _map(source, output)
    validate_package(result.package, resource_root=output)
    assert _codes(result) == {
        "INVENTOR_SEGMENT_MAJOR_UNSUPPORTED",
        "INVENTOR_PREVIEW_ONLY",
    }
    assert result.statistics["profile_unsupported"] is True
    (resource,) = result.package.resources
    assert resource.properties["role"] == "preview-image"
    assert resource.uri.startswith("preview/") and resource.uri.endswith(".png")
    assert (output / resource.uri).read_bytes() == PNG
    assert result.package.metadata["preview_resource_id"] == resource.id
    assert result.statistics["preview_byte_length"] == len(PNG)
    with pytest.raises(InventorReadError, match="unsupported save generation"):
        _map(source, tmp_path / "strict", ImportOptions(strict=True))


def test_states_without_bodies_each_have_one_error(tmp_path: Path) -> None:
    cases = {
        "INVENTOR_KIND_UNSUPPORTED": stand_in_source(
            tmp_path, kind="assembly", bodies=None
        ),
        "INVENTOR_GEOMETRY_UNAVAILABLE": stand_in_source(
            tmp_path, bodies=None, reasons=("no stored table was located",)
        ),
        "INVENTOR_KERNEL_MISSING": stand_in_source(tmp_path, kernel_missing=True),
        "INVENTOR_NO_GEOMETRY": stand_in_source(
            tmp_path,
            bodies=(
                rejected_body(
                    1, "unsupported", "geometry.surface_unsupported", "m", "helix"
                ),
            ),
        ),
    }
    for index, (code, source) in enumerate(cases.items()):
        result = _map(source, tmp_path / f"package-{index}")
        errors = [d.code for d in result.diagnostics if d.severity == "error"]
        assert errors == [code], code
        assert result.package.visualizations == [] and result.package.bodies == []
    assert "INVENTOR_BODY_UNSUPPORTED" in _codes(
        _map(cases["INVENTOR_NO_GEOMETRY"], tmp_path / "again")
    )
