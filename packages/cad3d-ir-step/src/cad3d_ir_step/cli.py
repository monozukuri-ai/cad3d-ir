"""Command-line conversion entry point for cad3d-ir-step."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from cad3d_ir import DirectoryResourceWriter, dump_manifest
from cad3d_ir.importers import ImportOptions

from cad3d_ir_step import StepImporter, StepImportError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cad3d-ir-step",
        description="Convert STEP AP242 product data to a cad3d-ir package directory.",
    )
    parser.add_argument("source", type=Path, help="input STEP AP242 Part 21 file")
    parser.add_argument("output", type=Path, help="output package directory")
    parser.add_argument(
        "--allow-non-ap242",
        action="store_true",
        help="allow other STEP schemas and emit a diagnostic",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="replace existing manifest and geometry resources",
    )
    parser.add_argument(
        "--diagnostics",
        type=Path,
        help="write structured import diagnostics as JSON",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the STEP adapter CLI."""
    args = _parser().parse_args(argv)
    manifest_path = args.output / "manifest.json"
    if manifest_path.exists() and not args.overwrite:
        print(f"error: manifest already exists: {manifest_path}", file=sys.stderr)
        return 2

    try:
        result = StepImporter().import_file(
            args.source,
            options=ImportOptions(strict=not args.allow_non_ap242),
            resources=DirectoryResourceWriter(
                args.output,
                overwrite=args.overwrite,
            ),
        )
        dump_manifest(result.package, manifest_path, pretty=True)
    except (StepImportError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.diagnostics is not None:
        args.diagnostics.parent.mkdir(parents=True, exist_ok=True)
        args.diagnostics.write_text(
            json.dumps(
                {
                    "diagnostics": [item.as_dict() for item in result.diagnostics],
                    "statistics": result.statistics,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )

    warning_count = sum(
        item.severity in {"warning", "error"} for item in result.diagnostics
    )
    print(
        f"wrote {manifest_path}: {len(result.package.product_definitions)} products, "
        f"{len(result.package.occurrences)} occurrences, {warning_count} warnings"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
