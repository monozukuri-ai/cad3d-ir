from __future__ import annotations

import json
from pathlib import Path

from cad3d_ir_icad.cli import main


def test_cli_writes_manifest_diagnostics_and_step(
    box_path: Path, tmp_path: Path, capsys
) -> None:
    output = tmp_path / "package"
    diagnostics = tmp_path / "diagnostics.json"
    step = tmp_path / "box.step"
    assert (
        main(
            [
                str(box_path),
                str(output),
                "--diagnostics",
                str(diagnostics),
                "--step",
                str(step),
                "--brep",
            ]
        )
        == 0
    )
    captured = capsys.readouterr().out
    assert "12 triangles, 1 of 2 parts with solids" in captured
    assert "1 solids, 0 skipped" in captured
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["format"] == "cad3d-ir"
    assert any(r["uri"].endswith(".brep") for r in manifest["resources"])
    payload = json.loads(diagnostics.read_text(encoding="utf-8"))
    assert payload["statistics"]["icd_version"] == "V7L7"
    assert {d["code"] for d in payload["diagnostics"]} >= {"ICD_SAVED_BREP"}
    assert step.stat().st_size > 0
    assert main([str(box_path), str(output)]) == 2
    assert main([str(box_path), str(output), "--overwrite", "--no-mesh"]) == 0


def test_cli_reports_failures(tmp_path: Path, capsys) -> None:
    assert main([str(tmp_path / "missing.icd"), str(tmp_path / "out")]) == 1
    assert "error:" in capsys.readouterr().err
