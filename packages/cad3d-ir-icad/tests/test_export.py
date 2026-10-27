from __future__ import annotations

import re
from pathlib import Path

import pytest
from cad3d_ir import DirectoryResourceWriter
from cad3d_ir.importers import ImportOptions

from cad3d_ir_icad import IcadExportError, export_step, read_source


def _product_names(text: str) -> list[str]:
    def decode(match: re.Match[str]) -> str:
        return bytes.fromhex(match.group(1)).decode("utf-16-be")

    return [
        re.sub(r"\\X2\\([0-9A-F]+)\\X0\\", decode, name)
        for name in re.findall(r"PRODUCT\('([^']*)'", text)
    ]


def test_export_step_writes_a_named_assembly_that_reads_back(
    box_path: Path, tmp_path: Path
) -> None:
    from cad3d_ir_step import StepImporter

    target = tmp_path / "box.step"
    result = export_step(box_path, target, schema="AP214")
    assert result.path == target and result.byte_length == target.stat().st_size
    assert result.solids == 1 and result.parts == 2 and result.shared_definitions == 1
    assert result.bodies == 1
    assert result.skipped == () and result.version_label == "V7L7"
    text = target.read_text(encoding="utf-8", errors="replace")
    assert "AUTOMOTIVE_DESIGN" in text
    # One product per distinct solid, named after its part (OCCT may write the
    # non-ASCII name as an ISO 10303-21 \X2\ escape; 部 = U+90E8, 品 = U+54C1).
    assert _product_names(text) == ["box-v7l7", "部品"]
    imported = StepImporter(body_mesh_resources=False).import_file(
        target,
        options=ImportOptions(include_visualization_mesh=True, include_brep=False),
        resources=DirectoryResourceWriter(tmp_path / "back"),
    )
    (visualization,) = imported.package.visualizations
    assert visualization.statistics["triangles"] == 12
    assert visualization.bounds is not None
    assert visualization.bounds.max[0] - visualization.bounds.min[0] == pytest.approx(
        10_000.0, abs=1e-3
    )
    with pytest.raises(IcadExportError, match="exists"):
        export_step(box_path, target)


def test_export_step_accepts_a_read_source_and_refuses_empty_models(
    box_path: Path, external_path: Path, v8l4_path: Path, tmp_path: Path
) -> None:
    source = read_source(box_path)
    result = export_step(source, tmp_path / "from-source.step", schema="AP203")
    assert result.schema == "AP203" and result.solids == 1
    with pytest.raises(IcadExportError, match="no part"):
        export_step(external_path, tmp_path / "external.step")
    with pytest.raises(IcadExportError, match="qualified"):
        export_step(v8l4_path, tmp_path / "v8l4.step")
    with pytest.raises(ValueError):
        export_step(box_path, tmp_path / "bad.step", schema="AP999")  # type: ignore[arg-type]
