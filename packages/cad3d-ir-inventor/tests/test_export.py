from __future__ import annotations

import re
from pathlib import Path

import pytest
from cad3d_ir import DirectoryResourceWriter
from cad3d_ir.importers import ImportOptions

from cad3d_ir_inventor import InventorExportError, export_step, read_source

from .conftest import converted_body, needs_kit, rejected_body, stand_in_source


def _product_names(text: str) -> list[str]:
    return re.findall(r"PRODUCT\('([^']*)'", text)


def _read_back(path: Path, output: Path):
    from cad3d_ir_step import StepImporter

    return StepImporter(body_mesh_resources=False).import_file(
        path,
        options=ImportOptions(include_visualization_mesh=True, include_brep=False),
        resources=DirectoryResourceWriter(output),
    )


def _volume(shape) -> float:
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    properties = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, properties)
    return float(properties.Mass())


def _step_volume(path: Path) -> float:
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.STEPControl import STEPControl_Reader

    reader = STEPControl_Reader()
    assert reader.ReadFile(str(path)) == IFSelect_RetDone
    reader.TransferRoots()
    return _volume(reader.OneShape())


@needs_kit
def test_export_step_writes_named_bodies_that_read_back(
    ring_path: Path, tmp_path: Path
) -> None:
    target = tmp_path / "ring.step"
    result = export_step(ring_path, target, schema="AP214")
    assert result.path == target and result.byte_length == target.stat().st_size
    assert result.bodies == 1 and result.approximated_bodies == 0
    assert result.skipped == () and result.major == 30
    text = target.read_text(encoding="utf-8", errors="replace")
    assert "AUTOMOTIVE_DESIGN" in text
    assert _product_names(text) == ["ring-r10-r4-h20", "Body_1"]
    assert _step_volume(target) == pytest.approx(5277.875658, rel=1e-6)
    imported = _read_back(target, tmp_path / "back")
    (visualization,) = imported.package.visualizations
    assert visualization.statistics["triangles"] > 0
    assert visualization.bounds is not None
    assert visualization.bounds.max[2] - visualization.bounds.min[2] == pytest.approx(
        20.0, abs=1e-6
    )
    with pytest.raises(InventorExportError, match="exists"):
        export_step(ring_path, target)


@needs_kit
def test_export_step_takes_a_read_source_and_another_schema(
    box_path: Path, tmp_path: Path
) -> None:
    source = read_source(box_path)
    result = export_step(source, tmp_path / "box.step", schema="AP242", name="箱")
    assert result.schema == "AP242" and result.unit == "mm"
    assert _step_volume(result.path) == pytest.approx(6000.0, rel=1e-6)
    with pytest.raises(ValueError):
        export_step(box_path, tmp_path / "bad.step", schema="AP999")  # type: ignore[arg-type]


def test_export_step_lists_the_bodies_it_leaves_out(tmp_path: Path) -> None:
    source = stand_in_source(
        tmp_path,
        bodies=(
            converted_body(1),
            rejected_body(
                2, "unsupported", "geometry.surface_unsupported", "face $29: thread"
            ),
            converted_body(3, approximations=({"type": "intcurve-curve"},)),
        ),
    )
    result = export_step(source, tmp_path / "part.step", name="part")
    assert result.bodies == 2 and result.approximated_bodies == 1
    assert result.skipped == (("Body_2", "face $29: thread"),)
    text = result.path.read_text(encoding="utf-8", errors="replace")
    assert _product_names(text) == ["part", "Body_1", "Body_3"]
    assert _step_volume(result.path) == pytest.approx(12000.0, rel=1e-6)


def test_export_step_refuses_documents_without_bodies(tmp_path: Path) -> None:
    empty = stand_in_source(tmp_path, bodies=None, reasons=("no stored table",))
    with pytest.raises(InventorExportError, match="not converted.*no stored table"):
        export_step(empty, tmp_path / "empty.step")
    none = stand_in_source(
        tmp_path,
        bodies=(rejected_body(1, "failed", "geometry.face_invalid", "face $1"),),
    )
    with pytest.raises(InventorExportError, match="no body"):
        export_step(none, tmp_path / "none.step")
