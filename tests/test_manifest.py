from __future__ import annotations

from cad3d_ir import (
    CadPackage,
    dump_manifest,
    dumps_manifest,
    load_manifest,
    loads_manifest,
)


def test_manifest_round_trip(sample_package: CadPackage) -> None:
    encoded = dumps_manifest(sample_package)

    assert loads_manifest(encoded) == sample_package
    assert encoded == dumps_manifest(sample_package)
    assert encoded.endswith("\n")


def test_pretty_manifest_round_trip(sample_package: CadPackage, tmp_path) -> None:
    manifest_path = tmp_path / "manifest.json"
    dump_manifest(sample_package, manifest_path, pretty=True)

    assert load_manifest(manifest_path) == sample_package
    assert manifest_path.read_text(encoding="utf-8").startswith("{\n  ")


def test_minimal_example_is_valid() -> None:
    package = load_manifest("examples/minimal/manifest.json")

    assert package.id == "package-minimal"
    assert package.product_definitions[0].kind == "part"
