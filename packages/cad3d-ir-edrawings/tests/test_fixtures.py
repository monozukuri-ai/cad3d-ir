"""Self-authored eDrawings 2026 fixtures: units, axes, names and grouping.

The files were saved by eDrawings 2026 from STEP sources the project generated
(an OCCT box) or owns (the cad3d-ir-step reference assembly), so bounds, unit
factors and names are known independently of edrawings-kit.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest
from cad3d_ir import DirectoryResourceWriter, validate_package
from cad3d_ir.importers import ImportOptions
from cad3d_ir.mesh import validate_glb_bytes

from cad3d_ir_edrawings import EdrawingsImporter

MESH = ImportOptions(include_visualization_mesh=True, include_brep=False)


def _json_chunk(payload: bytes) -> dict:
    length = struct.unpack_from("<I", payload, 12)[0]
    return json.loads(payload[20 : 20 + length].decode("utf-8"))


def _import(path: Path, output: Path):
    result = EdrawingsImporter().import_file(
        path, options=MESH, resources=DirectoryResourceWriter(output)
    )
    validate_package(result.package, resource_root=output)
    [visualization] = result.package.visualizations
    resource = next(
        r for r in result.package.resources if r.id == visualization.resource_id
    )
    payload = (output / resource.uri).read_bytes()
    report = validate_glb_bytes(payload)
    assert report.valid, report.errors
    return result, visualization, _json_chunk(payload)


@pytest.mark.parametrize(
    "name,units",
    [
        ("box-100x50x20mm.eprt", 1.0),
        ("box-100x50x20mm-as-m.eprt", 1000.0),
        ("box-100x50x20mm-as-inch.eprt", 25.4),
    ],
)
def test_boxes_in_three_unit_systems_land_on_the_same_millimetres(
    fixtures_dir: Path, tmp_path: Path, name: str, units: float
) -> None:
    result, visualization, document = _import(fixtures_dir / name, tmp_path / "out")
    assert result.statistics["model_units"] == units
    assert result.statistics["scale_mm_per_unit"] == units
    assert visualization.bounds is not None
    assert visualization.bounds.min == pytest.approx((0.0, 0.0, 0.0), abs=1e-3)
    assert visualization.bounds.max == pytest.approx((100.0, 50.0, 20.0), abs=1e-2)
    package = result.package
    root, part = package.product_definitions
    assert root.kind == "part" and root.name == Path(name).stem
    assert part.name == Path(name).stem  # generic "Body 0" names defer to the file
    assert len(package.occurrences) == 1 and len(package.bodies) == 6
    assert result.statistics["bodies"] == 6 and result.statistics["instances"] == 6
    assert visualization.statistics["triangles"] == 12
    assert visualization.statistics["nodes"] == 2
    assert result.statistics["hsf_version"] == "19.10"
    assert result.statistics["writer"].startswith("HoopsExchange Reader AddIn")
    assert result.statistics["preview_byte_length"] > 0
    assert package.source_documents[0].properties["mount_path"] == (
        "/include library/model_1_include"
    )
    codes = {item.code for item in result.diagnostics}
    assert {"EDRAWINGS_DISPLAY_MESH_ONLY", "EDRAWINGS_PLACEMENT_ESTIMATED"} <= codes
    assert "EDRAWINGS_NO_GEOMETRY" not in codes and "EDRAWINGS_STOPPED_AT" not in codes


def test_reference_assembly_groups_shells_into_parts_with_placements(
    fixtures_dir: Path, tmp_path: Path
) -> None:
    result, visualization, document = _import(
        fixtures_dir / "reference-assembly.easm", tmp_path / "out"
    )
    package = result.package
    root, *parts = package.product_definitions
    assert root.kind == "assembly" and root.name == "reference-assembly"
    by_name = {part.name: part for part in parts}
    assert set(by_name) == {"Base Plate", "Pin"}
    occurrences = [o for o in package.occurrences if o.product_id == by_name["Pin"].id]
    assert sorted(o.transform.matrix[0][3] for o in occurrences) == [10.0, 30.0]
    assert len(package.occurrences) == 3
    # Six plate shells and three pin shells share two products.
    pin_configuration = next(
        c for c in package.configurations if c.product_id == by_name["Pin"].id
    )
    plate_configuration = next(
        c for c in package.configurations if c.product_id == by_name["Base Plate"].id
    )
    assert len(pin_configuration.body_ids) == 3
    assert len(plate_configuration.body_ids) == 6
    assert visualization.bounds is not None
    assert visualization.bounds.max == pytest.approx((40.0, 20.0, 12.0), abs=1e-6)
    assert visualization.statistics["triangles"] == 516
    assert visualization.statistics["mesh_triangles"] == 264
    assert visualization.statistics["nodes"] == 4
    assert visualization.statistics["meshes"] == 2
    nodes = [n["name"] for n in document["nodes"][1:]]
    assert sorted(nodes) == ["Base Plate", "Pin", "Pin"]
    pins = [n for n in document["nodes"][1:] if n["name"] == "Pin"]
    assert pins[0]["mesh"] == pins[1]["mesh"]
    assert result.statistics["configurations"] == 1
    assert len(package.appearances) == 2
    codes = {item.code for item in result.diagnostics}
    assert "EDRAWINGS_CONFIGURATION_SELECTED" in codes


def test_japanese_component_names_survive(fixtures_dir: Path, tmp_path: Path) -> None:
    result, visualization, document = _import(
        fixtures_dir / "japanese-names-assembly.easm", tmp_path / "out"
    )
    names = {part.name for part in result.package.product_definitions[1:]}
    assert names == {"ベース板", "ピン_φ4"}
    assert sorted(n["name"] for n in document["nodes"][1:]) == [
        "ピン_φ4",
        "ピン_φ4",
        "ベース板",
    ]
    assert visualization.bounds is not None
    assert visualization.bounds.max == pytest.approx((40.0, 20.0, 12.0), abs=1e-6)
