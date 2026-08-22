#!/usr/bin/env python3
"""Generate and synchronize the public and packaged JSON Schema copies."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from cad3d_ir.constants import CURRENT_IR_VERSION  # noqa: E402
from cad3d_ir.model import CadPackage  # noqa: E402

TARGETS = (
    ROOT / "schema" / "cad3d-ir.schema.json",
    ROOT / "src" / "cad3d_ir" / "data" / "cad3d-ir.schema.json",
)


def generated_schema_text() -> str:
    """Return the canonical generated schema text."""
    generated = CadPackage.model_json_schema(
        by_alias=True,
        mode="validation",
        ref_template="#/$defs/{model}",
    )
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"urn:monozukuri-ai:cad3d-ir:schema:{CURRENT_IR_VERSION}",
        **generated,
    }
    return json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def check_schema(expected: str) -> int:
    """Return zero only when every checked-in schema copy is current."""
    stale: list[Path] = []
    for target in TARGETS:
        if not target.is_file() or target.read_text(encoding="utf-8") != expected:
            stale.append(target)
    if stale:
        for target in stale:
            print(f"stale schema: {target.relative_to(ROOT)}", file=sys.stderr)
        return 1
    return 0


def write_schema(expected: str) -> None:
    """Replace all generated schema copies."""
    for target in TARGETS:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(expected, encoding="utf-8", newline="\n")
        print(f"wrote {target.relative_to(ROOT)}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail when a checked-in schema differs from the generated schema",
    )
    args = parser.parse_args()
    expected = generated_schema_text()
    if args.check:
        return check_schema(expected)
    write_schema(expected)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
