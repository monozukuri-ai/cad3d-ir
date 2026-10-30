"""Optional check against local eDrawings files (never committed).

Set ``CAD3D_IR_EDRAWINGS_SAMPLES`` to a directory of ``.eprt``/``.easm`` files
to run it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from cad3d_ir import DirectoryResourceWriter, validate_package
from cad3d_ir.importers import ImportOptions
from cad3d_ir.mesh import validate_glb_bytes

from cad3d_ir_edrawings import EdrawingsImporter

SAMPLES = os.environ.get("CAD3D_IR_EDRAWINGS_SAMPLES")


@pytest.mark.skipif(not SAMPLES, reason="CAD3D_IR_EDRAWINGS_SAMPLES is not set")
def test_local_samples_import_and_validate(tmp_path: Path) -> None:
    root = Path(SAMPLES or "")
    paths = sorted(
        {
            *root.rglob("*.[eE][pP][rR][tT]"),
            *root.rglob("*.[eE][aA][sS][mM]"),
        }
    )
    assert paths, "no .eprt/.easm files found"
    for index, path in enumerate(paths):
        output = tmp_path / f"sample-{index}"
        result = EdrawingsImporter().import_file(
            path,
            options=ImportOptions(
                strict=False, include_visualization_mesh=True, include_brep=False
            ),
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
