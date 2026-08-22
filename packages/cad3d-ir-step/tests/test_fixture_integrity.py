from __future__ import annotations

from hashlib import sha256
from pathlib import Path

from native_fixture import load_native_document

_EXPECTED_HASHES = {
    "reference-assembly.step": (
        "ce52a8347e020edfde44305023aa882f465adb2c621b33651cb8fa5da4d8e76b"
    ),
    "native-reference.json": (
        "892958ef605ca82d9dbabd1b4811c5ac133ed4bf8d4b9fdac59385aa864d3e14"
    ),
    "expected-semantics.json": (
        "0a92ea6da6d69ad16263cfc8fe72ed6159c15133ad8949d693425b7f9fc397f8"
    ),
}


def test_fixture_hashes_and_native_source_schema(fixtures_dir: Path) -> None:
    for name, expected in _EXPECTED_HASHES.items():
        assert sha256((fixtures_dir / name).read_bytes()).hexdigest() == expected

    native = load_native_document(fixtures_dir / "native-reference.json")
    assert native.format == "synthetic-native-cad"
    assert native.root_product_key == "assembly"
    assert len(native.products) == 4
