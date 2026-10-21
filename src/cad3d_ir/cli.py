"""Command-line validation and schema inspection."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from cad3d_ir.importers import ImportOptions, MeshImporter, MeshImportError
from cad3d_ir.io import DirectoryResourceWriter, dump_manifest, load_manifest
from cad3d_ir.schema import load_schema
from cad3d_ir.validation import IRValidationError, validate_resources


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cad3d-ir",
        description="Inspect and validate cad3d-ir manifests.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser(
        "validate",
        help="validate a manifest and optionally its resource bytes",
    )
    validate_parser.add_argument("manifest", type=Path)
    validate_parser.add_argument(
        "--resources",
        action="store_true",
        help="validate resource files relative to the manifest directory",
    )
    validate_parser.add_argument(
        "--resource-root",
        type=Path,
        help="validate resources relative to this directory",
    )

    schema_parser = subparsers.add_parser(
        "schema",
        help="print or write the bundled canonical JSON Schema",
    )
    schema_parser.add_argument("-o", "--output", type=Path)

    import_parser = subparsers.add_parser(
        "import",
        help="convert an STL or OBJ mesh file to a package directory",
    )
    import_parser.add_argument("source", type=Path, help="input .stl or .obj file")
    import_parser.add_argument("output", type=Path, help="output package directory")
    import_parser.add_argument(
        "--overwrite",
        action="store_true",
        help="replace an existing manifest and geometry resources",
    )
    import_parser.add_argument(
        "--diagnostics",
        type=Path,
        help="write structured import diagnostics as JSON",
    )
    return parser


def _run_validate(args: argparse.Namespace) -> int:
    package = load_manifest(args.manifest)
    resource_root = args.resource_root
    if resource_root is None and args.resources:
        resource_root = args.manifest.parent
    if resource_root is not None:
        validate_resources(package, resource_root)
    print(f"valid {package.format} {package.version}: {package.id}")
    return 0


def _run_import(args: argparse.Namespace) -> int:
    manifest_path = args.output / "manifest.json"
    if manifest_path.exists() and not args.overwrite:
        print(f"error: manifest already exists: {manifest_path}", file=sys.stderr)
        return 2
    result = MeshImporter().import_file(
        args.source,
        options=ImportOptions(),
        resources=DirectoryResourceWriter(args.output, overwrite=args.overwrite),
    )
    dump_manifest(result.package, manifest_path, pretty=True)
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
        f"wrote {manifest_path}: {result.statistics['triangles']} triangles, "
        f"{result.statistics['line_segments']} line segments, "
        f"{warning_count} warnings"
    )
    return 0


def _run_schema(args: argparse.Namespace) -> int:
    text = (
        json.dumps(
            load_schema(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    if args.output is None:
        sys.stdout.write(text)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8", newline="\n")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command-line interface and return a process exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            return _run_validate(args)
        if args.command == "schema":
            return _run_schema(args)
        if args.command == "import":
            return _run_import(args)
    except (
        IRValidationError,
        MeshImportError,
        ValidationError,
        OSError,
        UnicodeError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    parser.error(f"unsupported command: {args.command}")
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
