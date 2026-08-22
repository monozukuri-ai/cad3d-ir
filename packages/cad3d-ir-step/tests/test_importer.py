from __future__ import annotations

from pathlib import Path

import pytest
from cad3d_ir import DirectoryResourceWriter, validate_package
from cad3d_ir.constants import CURRENT_IR_VERSION
from cad3d_ir.importers import Importer, ImportOptions
from OCP.BRep import BRep_Builder
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepTools import BRepTools
from OCP.TopoDS import TopoDS_Shape

from cad3d_ir_step import (
    StepImporter,
    UnsupportedIRVersionError,
    UnsupportedStepSchemaError,
)


def test_importer_implements_core_protocol() -> None:
    assert isinstance(StepImporter(), Importer)


def test_probe_recognizes_ap242(fixtures_dir: Path) -> None:
    importer = StepImporter()
    assert importer.probe(fixtures_dir / "reference-assembly.step") == 1.0
    assert importer.probe(fixtures_dir / "missing.step") == 0.0


def test_xde_import_preserves_reuse_and_local_placements(
    fixtures_dir: Path,
    tmp_path: Path,
) -> None:
    result = StepImporter().import_file(
        fixtures_dir / "reference-assembly.step",
        options=ImportOptions(),
        resources=DirectoryResourceWriter(tmp_path),
    )
    validate_package(result.package, resource_root=tmp_path)

    products = {product.id: product for product in result.package.product_definitions}
    products_by_name = {product.name: product for product in products.values()}
    assert set(products_by_name) == {
        "Reference Assembly",
        "Pin Pair",
        "Base Plate",
        "Pin",
    }
    assert products_by_name["Reference Assembly"].kind == "assembly"
    assert products_by_name["Pin Pair"].kind == "assembly"
    assert products_by_name["Pin"].kind == "part"

    pin_occurrences = [
        occurrence
        for occurrence in result.package.occurrences
        if occurrence.product_id == products_by_name["Pin"].id
    ]
    assert [occurrence.name for occurrence in pin_occurrences] == ["Pin:1", "Pin:2"]
    assert [
        tuple(row[3] for row in occurrence.transform.matrix[:3])
        for occurrence in pin_occurrences
    ] == [(0.0, 0.0, 0.0), (20.0, 0.0, 0.0)]

    assert result.statistics["products"] == 4
    assert result.statistics["occurrences"] == 4
    assert result.statistics["source_length_units"] == ["millimetre"]
    assert result.package.source_documents[0].properties["source_length_units"] == [
        "millimetre"
    ]
    assert [item.code for item in result.diagnostics] == [
        "STEP_CONFIGURATIONS_SYNTHESIZED"
    ]
    assert all(resource.byte_length > 0 for resource in result.package.resources)
    assert all(resource.uri.endswith(".brep") for resource in result.package.resources)
    for resource in result.package.resources:
        shape = TopoDS_Shape()
        assert BRepTools.Read_s(shape, str(tmp_path / resource.uri), BRep_Builder())
        assert not shape.IsNull()
        assert BRepCheck_Analyzer(shape).IsValid()


def test_strict_mode_rejects_non_ap242_before_geometry_transfer(
    tmp_path: Path,
) -> None:
    source = tmp_path / "ap214.step"
    source.write_text(
        "ISO-10303-21;\nHEADER;\n"
        "FILE_SCHEMA(('AUTOMOTIVE_DESIGN'));\n"
        "ENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n",
        encoding="ascii",
    )
    with pytest.raises(UnsupportedStepSchemaError, match="AP242 FILE_SCHEMA"):
        StepImporter().import_file(
            source,
            options=ImportOptions(strict=True),
            resources=DirectoryResourceWriter(tmp_path / "out"),
        )


def test_rejects_an_unsupported_target_ir_version(
    fixtures_dir: Path,
    tmp_path: Path,
) -> None:
    unsupported = "0.2.0" if CURRENT_IR_VERSION != "0.2.0" else "0.3.0"
    with pytest.raises(UnsupportedIRVersionError, match=unsupported):
        StepImporter().import_file(
            fixtures_dir / "reference-assembly.step",
            options=ImportOptions(ir_version=unsupported),
            resources=DirectoryResourceWriter(tmp_path),
        )
