"""Optional check against local iCAD SX files (never committed).

Set ``CAD3D_IR_ICAD_SAMPLES`` to a directory of ``.icd`` files to run it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from cad3d_ir import DirectoryResourceWriter, validate_package
from cad3d_ir.importers import ImportOptions
from cad3d_ir.mesh import validate_glb_bytes

from cad3d_ir_icad import IcadImporter

SAMPLES = os.environ.get("CAD3D_IR_ICAD_SAMPLES")


@pytest.mark.skipif(not SAMPLES, reason="CAD3D_IR_ICAD_SAMPLES is not set")
def test_local_samples_import_and_validate(tmp_path: Path) -> None:
    paths = sorted(Path(SAMPLES or "").rglob("*.[iI][cC][dD]"))
    assert paths, "no .icd files found"
    for index, path in enumerate(paths):
        output = tmp_path / f"sample-{index}"
        result = IcadImporter().import_file(
            path,
            options=ImportOptions(include_visualization_mesh=True, include_brep=False),
            resources=DirectoryResourceWriter(output),
        )
        validate_package(result.package, resource_root=output)
        for visualization in result.package.visualizations:
            resource = next(
                item
                for item in result.package.resources
                if item.id == visualization.resource_id
            )
            report = validate_glb_bytes((output / resource.uri).read_bytes())
            assert report.valid, (path, report.errors)
