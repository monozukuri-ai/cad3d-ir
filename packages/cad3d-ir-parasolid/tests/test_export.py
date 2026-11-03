from __future__ import annotations

import re
from pathlib import Path

import pytest
from cad3d_ir import DirectoryResourceWriter
from cad3d_ir.importers import ImportOptions

from cad3d_ir_parasolid import ParasolidExportError, export_step, read_source


def _product_names(text: str) -> list[str]:
    def decode(match: re.Match[str]) -> str:
        return bytes.fromhex(match.group(1)).decode("utf-16-be")

    return [
        re.sub(r"\\X2\\([0-9A-F]+)\\X0\\", decode, name)
        for name in re.findall(r"PRODUCT\('([^']*)'", text)
    ]


def test_export_step_writes_a_named_coloured_body_that_reads_back(
    box_path: Path, tmp_path: Path
) -> None:
    from cad3d_ir_step import StepImporter

    target = tmp_path / "box.step"
    result = export_step(box_path, target, schema="AP214")
    assert result.path == target and result.byte_length == target.stat().st_size
    assert result.bodies == 1 and result.placed == 1
    assert result.coloured_bodies == 1 and result.coloured_faces == 0
    assert result.skipped == () and result.schema_key == "SCH_3000000_30000"
    text = target.read_text(encoding="utf-8", errors="replace")
    assert "AUTOMOTIVE_DESIGN" in text and "COLOUR_RGB" in text
    assert _product_names(text) == ["onshape-box-v30", "M1 Box"]
    imported = StepImporter(body_mesh_resources=False).import_file(
        target,
        options=ImportOptions(include_visualization_mesh=True, include_brep=False),
        resources=DirectoryResourceWriter(tmp_path / "back"),
    )
    (visualization,) = imported.package.visualizations
    assert visualization.statistics["triangles"] == 12
    assert visualization.bounds is not None
    assert visualization.bounds.max[2] - visualization.bounds.min[2] == pytest.approx(
        30.0, abs=1e-6
    )
    with pytest.raises(ParasolidExportError, match="exists"):
        export_step(box_path, target)


def test_export_step_places_the_instances_and_colours_the_face(
    assembly_path: Path, tmp_path: Path
) -> None:
    from cad3d_ir_step import StepImporter

    source = read_source(assembly_path)
    result = export_step(source, tmp_path / "assembly.step", schema="AP203")
    assert result.schema == "AP203"
    assert result.bodies == 1 and result.placed == 2
    assert result.coloured_bodies == 0 and result.coloured_faces == 1
    imported = StepImporter(body_mesh_resources=False).import_file(
        result.path,
        options=ImportOptions(include_visualization_mesh=True, include_brep=False),
        resources=DirectoryResourceWriter(tmp_path / "back"),
    )
    (visualization,) = imported.package.visualizations
    assert visualization.statistics["triangles"] > 0
    # The placed disc sits 3 m above the other one.
    assert visualization.bounds is not None
    assert visualization.bounds.max[2] - visualization.bounds.min[2] == pytest.approx(
        3000.0, abs=1e-3
    )


def test_export_step_refuses_unreadable_files_and_bad_schemas(
    box_path: Path, unsupported_path: Path, tmp_path: Path
) -> None:
    with pytest.raises(ParasolidExportError, match="could not read"):
        export_step(unsupported_path, tmp_path / "unsupported.step")
    with pytest.raises(ValueError):
        export_step(box_path, tmp_path / "bad.step", schema="AP999")  # type: ignore[arg-type]
