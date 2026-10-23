from __future__ import annotations

import json
from pathlib import Path

from cad3d_ir import load_manifest

from cad3d_ir_sldprt.cli import main
from tests.synthetic import synthetic_document


def test_cli_writes_manifest_resources_and_diagnostics(
    part_path: Path, tmp_path: Path, capsys
) -> None:
    output = tmp_path / "package"
    diagnostics = tmp_path / "diagnostics.json"
    assert main([str(part_path), str(output), "--diagnostics", str(diagnostics)]) == 0
    package = load_manifest(output / "manifest.json")
    assert package.visualizations
    assert (output / package.resources[0].uri).is_file()
    report = json.loads(diagnostics.read_text(encoding="utf-8"))
    assert report["statistics"]["triangles"] == 3
    assert any(d["code"] == "SLDPRT_DISPLAY_MESH_ONLY" for d in report["diagnostics"])
    assert "3 triangles" in capsys.readouterr().out

    assert main([str(part_path), str(output)]) == 2
    assert main([str(part_path), str(output), "--overwrite", "--no-mesh"]) == 0
    assert load_manifest(output / "manifest.json").visualizations == []


def test_cli_reports_failures(tmp_path: Path, capsys) -> None:
    drawing = tmp_path / "sheet.SLDDRW"
    drawing.write_bytes(synthetic_document("DRAWING"))
    assert main([str(drawing), str(tmp_path / "out")]) == 1
    assert "drawings" in capsys.readouterr().err
    empty = tmp_path / "empty.SLDPRT"
    empty.write_bytes(synthetic_document(display=False))
    assert main([str(empty), str(tmp_path / "strict"), "--strict"]) == 1
    assert main([str(empty), str(tmp_path / "lenient")]) == 0
