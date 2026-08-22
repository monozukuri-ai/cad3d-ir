"""Load the checked-in and packaged canonical JSON Schema."""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path
from typing import Any


def load_schema(path: str | Path | None = None) -> dict[str, Any]:
    """Load an explicit schema or the copy bundled in the installed wheel."""
    if path is not None:
        return json.loads(Path(path).read_text(encoding="utf-8"))

    resource = files("cad3d_ir.data").joinpath("cad3d-ir.schema.json")
    return json.loads(resource.read_text(encoding="utf-8"))
