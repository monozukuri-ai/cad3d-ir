from __future__ import annotations

import json
from pathlib import Path

from cad3d_ir_inventor.cli import main

from .conftest import needs_kit


@needs_kit
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
    assert "12 triangles, 1 of 1 bodies converted" in captured
    assert "1 bodies, 0 skipped" in captured
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["format"] == "cad3d-ir"
    assert any(r["uri"].endswith(".glb") for r in manifest["resources"])
    assert any(r["uri"].endswith(".brep") for r in manifest["resources"])
    payload = json.loads(diagnostics.read_text(encoding="utf-8"))
    assert payload["statistics"]["major"] == 31
    assert {d["code"] for d in payload["diagnostics"]} >= {"INVENTOR_EXACT_BREP"}
    assert step.stat().st_size > 0
    assert main([str(box_path), str(output)]) == 2
    assert main([str(box_path), str(output), "--overwrite", "--no-mesh"]) == 0


@needs_kit
def test_cli_reports_failures(tmp_path: Path, box_path: Path, capsys) -> None:
    assert main([str(tmp_path / "missing.ipt"), str(tmp_path / "out")]) == 1
    assert "error:" in capsys.readouterr().err
    damaged = tmp_path / "damaged.ipt"
    damaged.write_bytes(box_path.read_bytes()[:8] + b"\0" * 2048)
    assert main([str(damaged), str(tmp_path / "out2"), "--strict"]) == 1
    assert "unreadable document" in capsys.readouterr().err
