from __future__ import annotations

import json
from pathlib import Path

from cad3d_ir import load_manifest, validate_package

from cad3d_ir_step.cli import main


def test_cli_writes_a_valid_package_and_diagnostics(
    fixtures_dir: Path,
    tmp_path: Path,
    capsys,
) -> None:
    output = tmp_path / "package"
    diagnostics = tmp_path / "diagnostics.json"
    assert (
        main(
            [
                str(fixtures_dir / "reference-assembly.step"),
                str(output),
                "--diagnostics",
                str(diagnostics),
            ]
        )
        == 0
    )

    package = load_manifest(output / "manifest.json")
    validate_package(package, resource_root=output)
    report = json.loads(diagnostics.read_text(encoding="utf-8"))
    assert report["statistics"]["products"] == 4
    assert report["diagnostics"][0]["code"] == "STEP_CONFIGURATIONS_SYNTHESIZED"
    assert "4 products, 4 occurrences, 0 warnings" in capsys.readouterr().out

    assert main([str(fixtures_dir / "reference-assembly.step"), str(output)]) == 2
    assert "manifest already exists" in capsys.readouterr().err
