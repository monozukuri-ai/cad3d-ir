from __future__ import annotations

import json
from pathlib import Path

from cad3d_ir import DirectoryResourceWriter, validate_package
from cad3d_ir.importers import ImportOptions
from native_fixture import map_native_document
from semantic import semantic_projection

from cad3d_ir_step import StepImporter


def test_step_and_native_sources_map_to_the_same_common_semantics(
    fixtures_dir: Path,
    tmp_path: Path,
) -> None:
    step_root = tmp_path / "step"
    step_result = StepImporter().import_file(
        fixtures_dir / "reference-assembly.step",
        options=ImportOptions(),
        resources=DirectoryResourceWriter(step_root),
    )
    validate_package(step_result.package, resource_root=step_root)

    native_root = tmp_path / "native"
    native_package = map_native_document(
        fixtures_dir / "native-reference.json",
        resources=DirectoryResourceWriter(native_root),
    )
    validate_package(native_package, resource_root=native_root)

    expected = json.loads(
        (fixtures_dir / "expected-semantics.json").read_text(encoding="utf-8")
    )
    assert semantic_projection(step_result.package) == expected
    assert semantic_projection(native_package) == expected
