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


def test_cli_imports_iges_with_meshes(
    fixtures_dir: Path, tmp_path: Path, capsys
) -> None:
    output = tmp_path / "iges"
    assert (
        main(
            [
                str(fixtures_dir / "reference-assembly.iges"),
                str(output),
                "--mesh",
                "--no-brep",
                "--deflection-ratio",
                "0.005",
            ]
        )
        == 0
    )
    package = load_manifest(output / "manifest.json")
    validate_package(package, resource_root=output)
    assert package.source_documents[0].format == "iges"
    uris = [resource.uri for resource in package.resources]
    assert len(uris) == 2  # one flat IGES part mesh + the package preview
    assert uris[-1] == "preview/reference-assembly.glb"
    assert all(uri.endswith(".glb") for uri in uris)
    assert package.visualizations[0].kind == "glb"
    assert "products" in capsys.readouterr().out


def test_cli_require_ap242_rejects_ap214(
    fixtures_dir: Path, tmp_path: Path, capsys
) -> None:
    assert (
        main(
            [
                str(fixtures_dir / "reference-assembly-ap214.step"),
                str(tmp_path / "out"),
                "--require-ap242",
            ]
        )
        == 1
    )
    assert "AP242 import was required" in capsys.readouterr().err
