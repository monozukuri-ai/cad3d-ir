from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import jsonschema

from cad3d_ir import CadPackage, load_schema, manifest_dict


def test_generated_schema_is_in_sync() -> None:
    subprocess.run(
        [sys.executable, "scripts/sync_schema.py", "--check"],
        check=True,
    )


def test_public_and_packaged_schema_match() -> None:
    public = json.loads(Path("schema/cad3d-ir.schema.json").read_text(encoding="utf-8"))

    assert load_schema() == public
    assert public["$id"].endswith(":0.1.0")
    assert set(public["required"]) >= {
        "format",
        "version",
        "id",
        "units",
        "coordinate_system",
    }


def test_generated_schema_accepts_manifest(sample_package: CadPackage) -> None:
    jsonschema.Draft202012Validator(load_schema()).validate(
        manifest_dict(sample_package)
    )
