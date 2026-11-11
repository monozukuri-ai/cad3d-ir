"""Optional check against local Inventor parts (never committed).

Set ``CAD3D_IR_INVENTOR_SAMPLES`` to a directory of IPT files to run it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from cad3d_ir import DirectoryResourceWriter, validate_package
from cad3d_ir.importers import ImportOptions
from cad3d_ir.mesh import validate_glb_bytes

from cad3d_ir_inventor import InventorImporter

SAMPLES = os.environ.get("CAD3D_IR_INVENTOR_SAMPLES")
PATTERNS = ("*.ipt", "*.IPT")


@pytest.mark.skipif(not SAMPLES, reason="CAD3D_IR_INVENTOR_SAMPLES is not set")
def test_local_samples_import_and_validate(tmp_path: Path) -> None:
    root = Path(SAMPLES or "")
    paths = sorted({path for pattern in PATTERNS for path in root.rglob(pattern)})
    assert paths, "no Inventor parts found"
    for index, path in enumerate(paths):
        output = tmp_path / f"sample-{index}"
        result = InventorImporter().import_file(
            path,
            options=ImportOptions(
                include_visualization_mesh=True, include_brep=False, strict=False
            ),
            resources=DirectoryResourceWriter(output),
        )
        validate_package(result.package, resource_root=output)
        statistics = result.statistics
        assert statistics["bodies"] == (
            statistics["bodies_converted"]
            + statistics["bodies_open_shell"]
            + statistics["bodies_unsupported"]
            + statistics["bodies_failed"]
        ), path
        for visualization in result.package.visualizations:
            resource = next(
                item
                for item in result.package.resources
                if item.id == visualization.resource_id
            )
            report = validate_glb_bytes((output / resource.uri).read_bytes())
            assert report.valid, (path, report.errors)
