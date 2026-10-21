from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest
from cad3d_ir import DirectoryResourceWriter, validate_package
from cad3d_ir.constants import CURRENT_IR_VERSION
from cad3d_ir.importers import Importer, ImportOptions
from cad3d_ir.mesh import validate_glb_bytes
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
    assert importer.probe(fixtures_dir / "reference-assembly.iges") == 0.0


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


def test_require_ap242_rejects_other_schemas_before_geometry_transfer(
    tmp_path: Path,
) -> None:
    source = tmp_path / "ap214.step"
    source.write_text(
        "ISO-10303-21;\nHEADER;\n"
        "FILE_SCHEMA(('AUTOMOTIVE_DESIGN'));\n"
        "ENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n",
        encoding="ascii",
    )
    with pytest.raises(UnsupportedStepSchemaError, match="AP242 import was required"):
        StepImporter(require_ap242=True).import_file(
            source,
            options=ImportOptions(strict=True),
            resources=DirectoryResourceWriter(tmp_path / "out"),
        )


def test_ap214_fixture_imports_with_an_informational_diagnostic(
    fixtures_dir: Path,
    tmp_path: Path,
) -> None:
    result = StepImporter().import_file(
        fixtures_dir / "reference-assembly-ap214.step",
        options=ImportOptions(),
        resources=DirectoryResourceWriter(tmp_path),
    )
    validate_package(result.package, resource_root=tmp_path)
    assert result.package.source_documents[0].format == "step"
    assert result.statistics["step_schemas"] == [
        "AUTOMOTIVE_DESIGN_CC2 { 1 2 10303 214 -1 1 5 4 }"
    ]
    assert [item.code for item in result.diagnostics] == [
        "STEP_SCHEMA_NOT_AP242",
        "STEP_CONFIGURATIONS_SYNTHESIZED",
    ]
    assert result.diagnostics[0].severity == "info"
    assert {product.name for product in result.package.product_definitions} == {
        "Reference Assembly",
        "Pin Pair",
        "Base Plate",
        "Pin",
    }
    assert StepImporter().probe(fixtures_dir / "reference-assembly-ap214.step") == 0.9


def test_visualization_mesh_is_written_per_body_and_per_package(
    fixtures_dir: Path,
    tmp_path: Path,
) -> None:
    result = StepImporter().import_file(
        fixtures_dir / "reference-assembly.step",
        options=ImportOptions(include_visualization_mesh=True),
        resources=DirectoryResourceWriter(tmp_path),
    )
    package = result.package
    validate_package(package, resource_root=tmp_path)

    kinds = sorted(
        (representation.kind, representation.role)
        for representation in package.representations
    )
    assert kinds == [
        ("brep", "design"),
        ("brep", "design"),
        ("mesh", "visualization"),
        ("mesh", "visualization"),
    ]
    mesh_representations = [
        item for item in package.representations if item.kind == "mesh"
    ]
    for representation in mesh_representations:
        assert representation.fidelity == "derived"
        assert representation.derivation is not None
        assert representation.derivation.relation == "tessellated"
        assert representation.derivation.source_representation_id in {
            item.id for item in package.representations if item.kind == "brep"
        }
        assert representation.derivation.tolerance is not None
        assert representation.bounds is not None
    for body in package.bodies:
        assert len(body.representation_ids) == 2

    assert len(package.visualizations) == 1
    visualization = package.visualizations[0]
    assert visualization.kind == "glb" and visualization.scope == "package"
    assert visualization.units is not None and visualization.units.length_unit == "mm"
    assert visualization.statistics["meshes"] == 2
    assert visualization.statistics["nodes"] == 1 + 5  # root + 5 XDE nodes
    assert visualization.statistics["triangles"] > 0
    assert visualization.statistics["line_segments"] > 0
    assert visualization.bounds is not None
    assert visualization.bounds.min == pytest.approx((0.0, 0.0, 0.0), abs=1e-6)
    assert visualization.bounds.max == pytest.approx((40.0, 20.0, 12.0), abs=1e-6)

    resource = next(
        item for item in package.resources if item.id == visualization.resource_id
    )
    assert resource.media_type == "model/gltf-binary"
    assert resource.uri == "preview/reference-assembly.glb"
    payload = (tmp_path / resource.uri).read_bytes()
    report = validate_glb_bytes(payload)
    assert report.valid, report.errors
    assert report.mesh_count == 2
    assert report.node_count == 6
    document = json.loads(
        payload[20 : 20 + struct.unpack_from("<I", payload, 12)[0]].decode("utf-8")
    )
    names = [node["name"] for node in document["nodes"]]
    assert names == [
        "Reference Assembly",
        "Reference Assembly",
        "Base Plate:1",
        "Pin Pair:1",
        "Pin:1",
        "Pin:2",
    ]
    pin_nodes = [node for node in document["nodes"] if node["name"].startswith("Pin:")]
    assert pin_nodes[0]["mesh"] == pin_nodes[1]["mesh"]
    assert pin_nodes[1]["matrix"][12:15] == [20.0, 0.0, 0.0]
    occurrence_ids = {occurrence.id for occurrence in package.occurrences}
    assert {node["extras"]["id"] for node in pin_nodes} <= occurrence_ids
    primitives = document["meshes"][0]["primitives"]
    assert [primitive["mode"] for primitive in primitives] == [4, 1]
    assert primitives[0]["extras"]["entityKind"] == "face"
    assert all(len(item) == 3 for item in primitives[0]["extras"]["ranges"])
    assert result.statistics["mesh_representations"] == 2
    assert result.statistics["visualization"]["byte_length"] == len(payload)
    assert [item.code for item in result.diagnostics] == [
        "STEP_CONFIGURATIONS_SYNTHESIZED"
    ]


def test_mesh_only_import_skips_brep_and_body_resources(
    fixtures_dir: Path,
    tmp_path: Path,
) -> None:
    result = StepImporter(body_mesh_resources=False).import_file(
        fixtures_dir / "reference-assembly.step",
        options=ImportOptions(
            include_visualization_mesh=True,
            include_brep=False,
            mesh_include_edges=False,
            mesh_linear_deflection_ratio=0.01,
        ),
        resources=DirectoryResourceWriter(tmp_path),
    )
    package = result.package
    validate_package(package, resource_root=tmp_path)
    assert package.representations == []
    assert all(body.representation_ids == [] for body in package.bodies)
    assert [resource.uri for resource in package.resources] == [
        "preview/reference-assembly.glb"
    ]
    assert package.visualizations[0].statistics["line_segments"] == 0
    assert result.statistics["brep_representations"] == 0
    assert result.statistics["resources"] == 1


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
