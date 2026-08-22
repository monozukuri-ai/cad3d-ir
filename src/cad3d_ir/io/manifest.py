"""Deterministic manifest JSON loading and dumping."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cad3d_ir.model import CadPackage
from cad3d_ir.validation import validate_graph


def manifest_dict(package: CadPackage, *, validate: bool = True) -> dict[str, Any]:
    """Return the JSON-compatible manifest object."""
    if validate:
        validate_graph(package)
    return package.model_dump(mode="json", exclude_none=True)


def dumps_manifest(
    package: CadPackage,
    *,
    pretty: bool = False,
    validate: bool = True,
) -> str:
    """Serialize a manifest with stable key ordering and a final newline."""
    document = manifest_dict(package, validate=validate)
    options: dict[str, Any] = {
        "allow_nan": False,
        "ensure_ascii": False,
        "sort_keys": True,
    }
    if pretty:
        options["indent"] = 2
    else:
        options["separators"] = (",", ":")
    return json.dumps(document, **options) + "\n"


def dump_manifest(
    package: CadPackage,
    path: str | Path,
    *,
    pretty: bool = False,
    validate: bool = True,
) -> None:
    """Write a deterministic UTF-8 manifest file."""
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        dumps_manifest(package, pretty=pretty, validate=validate),
        encoding="utf-8",
        newline="\n",
    )


def loads_manifest(data: str | bytes, *, validate: bool = True) -> CadPackage:
    """Parse a manifest from UTF-8 bytes or text."""
    if isinstance(data, bytes):
        data = data.decode("utf-8")
    package = CadPackage.model_validate_json(data)
    if validate:
        validate_graph(package)
    return package


def load_manifest(path: str | Path, *, validate: bool = True) -> CadPackage:
    """Load and validate a manifest file."""
    return loads_manifest(Path(path).read_bytes(), validate=validate)
