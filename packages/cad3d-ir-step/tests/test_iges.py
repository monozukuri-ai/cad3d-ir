from __future__ import annotations

from pathlib import Path

import pytest
from cad3d_ir import DirectoryResourceWriter, validate_package
from cad3d_ir.importers import Importer, ImportOptions
from cad3d_ir.mesh import validate_glb_bytes
from semantic import semantic_projection

from cad3d_ir_step import IgesImporter, StepImporter, StepReadError, inspect_iges_header


def test_iges_importer_implements_core_protocol() -> None:
    assert isinstance(IgesImporter(), Importer)


def test_iges_header_inspection(fixtures_dir: Path, tmp_path: Path) -> None:
    header = inspect_iges_header(fixtures_dir / "reference-assembly.iges")
    assert header.is_iges
    assert header.unit_flag == 2
    assert header.length_unit == "millimetre"
    assert header.product == "Open CASCADE IGES processor 7.9"
    assert IgesImporter().probe(fixtures_dir / "reference-assembly.iges") == 1.0
    assert IgesImporter().probe(fixtures_dir / "reference-assembly.step") == 0.0
    assert IgesImporter().probe(fixtures_dir / "missing.iges") == 0.0
    other = tmp_path / "notes.igs"
    other.write_text("hello\n", encoding="ascii")
    assert IgesImporter().probe(other) == pytest.approx(0.3)
    assert not inspect_iges_header(other).is_iges


def test_iges_import_matches_step_semantics_and_bounds(
    fixtures_dir: Path,
    tmp_path: Path,
) -> None:
    iges_root = tmp_path / "iges"
    result = IgesImporter().import_file(
        fixtures_dir / "reference-assembly.iges",
        options=ImportOptions(include_visualization_mesh=True),
        resources=DirectoryResourceWriter(iges_root),
    )
    validate_package(result.package, resource_root=iges_root)
    step_result = StepImporter().import_file(
        fixtures_dir / "reference-assembly.step",
        options=ImportOptions(include_visualization_mesh=True),
        resources=DirectoryResourceWriter(tmp_path / "step"),
    )

    package = result.package
    assert package.source_documents[0].format == "iges"
    assert result.statistics["source_length_units"] == ["millimetre"]
    # IGES carries no assembly structure: the fixture round-trips as one part.
    assert [product.kind for product in package.product_definitions] == ["part"]
    assert result.statistics["parts"] == 1
    assert package.visualizations[0].bounds is not None
    assert package.visualizations[0].bounds.min == pytest.approx(
        step_result.package.visualizations[0].bounds.min, abs=1e-6
    )
    assert package.visualizations[0].bounds.max == pytest.approx(
        step_result.package.visualizations[0].bounds.max, abs=1e-6
    )
    preview_uri = next(
        item.uri
        for item in package.resources
        if item.id == package.visualizations[0].resource_id
    )
    report = validate_glb_bytes((iges_root / preview_uri).read_bytes())
    assert report.valid, report.errors
    # The STEP preview shares one Pin mesh between two occurrences; the flat
    # IGES solid carries both pins, so it has at least as many triangles.
    assert (
        report.triangle_count
        >= step_result.package.visualizations[0].statistics["triangles"]
    )
    codes = [item.code for item in result.diagnostics]
    assert "IGES_CONFIGURATIONS_SYNTHESIZED" in codes
    assert all(code.startswith("IGES_") for code in codes)
    projection = semantic_projection(package)
    assert projection["products"]


def test_iges_rejects_non_iges_input(tmp_path: Path) -> None:
    source = tmp_path / "notes.igs"
    source.write_text("hello\n", encoding="ascii")
    with pytest.raises(StepReadError, match="not an IGES file"):
        IgesImporter().import_file(
            source,
            options=ImportOptions(),
            resources=DirectoryResourceWriter(tmp_path / "out"),
        )
