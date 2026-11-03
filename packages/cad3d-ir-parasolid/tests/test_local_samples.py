"""Optional check against local transmit files (never committed).

Set ``CAD3D_IR_PARASOLID_SAMPLES`` to a directory of X_T / X_B files to run it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from cad3d_ir import DirectoryResourceWriter, validate_package
from cad3d_ir.importers import ImportOptions
from cad3d_ir.mesh import validate_glb_bytes

from cad3d_ir_parasolid import ParasolidImporter

SAMPLES = os.environ.get("CAD3D_IR_PARASOLID_SAMPLES")
PATTERNS = ("*.x_t", "*.x_b", "*.X_T", "*.X_B", "*.xmt_txt", "*.xmt_bin")


@pytest.mark.skipif(not SAMPLES, reason="CAD3D_IR_PARASOLID_SAMPLES is not set")
def test_local_samples_import_and_validate(tmp_path: Path) -> None:
    root = Path(SAMPLES or "")
    paths = sorted({path for pattern in PATTERNS for path in root.rglob(pattern)})
    assert paths, "no transmit files found"
    for index, path in enumerate(paths):
        output = tmp_path / f"sample-{index}"
        result = ParasolidImporter().import_file(
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
