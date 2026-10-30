from __future__ import annotations

import json
from pathlib import Path

from cad3d_ir import load_manifest

from cad3d_ir_edrawings.cli import main
from tests.synthetic import configurations_hsf, empty_hsf, model_hsf


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
    assert report["statistics"]["triangles"] == 36
    assert any(
        d["code"] == "EDRAWINGS_DISPLAY_MESH_ONLY" for d in report["diagnostics"]
    )
    assert "36 triangles, 3 placements of 2 parts" in capsys.readouterr().out

    assert main([str(part_path), str(output)]) == 2
    assert main([str(part_path), str(output), "--overwrite", "--no-mesh"]) == 0
    assert load_manifest(output / "manifest.json").visualizations == []


def test_cli_reports_failures(tmp_path: Path, capsys) -> None:
    drawing = tmp_path / "sheet.edrw"
    drawing.write_bytes(model_hsf())
    assert main([str(drawing), str(tmp_path / "out")]) == 1
    assert "accept_drawings" in capsys.readouterr().err
    assert main([str(drawing), str(tmp_path / "ok"), "--accept-drawings"]) == 0
    empty = tmp_path / "empty.eprt"
    empty.write_bytes(empty_hsf())
    assert main([str(empty), str(tmp_path / "strict"), "--strict"]) == 1
    assert main([str(empty), str(tmp_path / "lenient")]) == 0
    configs = tmp_path / "configs.easm"
    configs.write_bytes(configurations_hsf())
    assert (
        main([str(configs), str(tmp_path / "gamma"), "--configuration", "Gamma"]) == 1
    )
    assert "configuration_unknown" in capsys.readouterr().err
