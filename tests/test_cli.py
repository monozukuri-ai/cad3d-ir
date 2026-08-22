from __future__ import annotations

from cad3d_ir import CadPackage, dump_manifest
from cad3d_ir.cli import main


def test_validate_cli(
    sample_package: CadPackage,
    package_directory,
    capsys,
) -> None:
    manifest = package_directory / "manifest.json"
    dump_manifest(sample_package, manifest)

    result = main(["validate", str(manifest), "--resources"])

    assert result == 0
    assert "valid cad3d-ir 0.1.0" in capsys.readouterr().out


def test_validate_cli_reports_error(tmp_path, capsys) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text('{"id":"package-invalid"}', encoding="utf-8")

    result = main(["validate", str(manifest)])

    assert result == 1
    assert "error:" in capsys.readouterr().err
