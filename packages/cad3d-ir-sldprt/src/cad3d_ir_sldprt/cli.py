"""Command-line conversion entry point for cad3d-ir-sldprt."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from cad3d_ir import DirectoryResourceWriter, dump_manifest
from cad3d_ir.importers import ImportOptions

from cad3d_ir_sldprt import SldprtImporter, SldprtImportError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cad3d-ir-sldprt",
        description=(
            "Convert a SOLIDWORKS part (.SLDPRT) to a cad3d-ir package directory "
            "with a glTF binary of its saved display meshes."
        ),
    )
    parser.add_argument("source", type=Path, help="input .SLDPRT file")
    parser.add_argument("output", type=Path, help="output package directory")
    parser.add_argument(
        "--no-mesh",
        action="store_true",
        help="write metadata and the preview image only, without the display GLB",
    )
    parser.add_argument(
        "--no-edges",
        action="store_true",
        help="leave the face-outline LINES primitive out of the GLB",
    )
    parser.add_argument(
        "--no-preview",
        action="store_true",
        help="do not write the saved preview image",
    )
    parser.add_argument(
        "--no-assemblies",
        action="store_true",
        help="reject .SLDASM documents instead of placing their component instances",
    )
    parser.add_argument(
        "--limits",
        default="service",
        help="sldkit limit profile (default: service)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="fail when the document has no display data",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="replace existing manifest and resources",
    )
    parser.add_argument(
        "--diagnostics",
        type=Path,
        help="write structured import diagnostics as JSON",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the SLDPRT adapter CLI."""
    args = _parser().parse_args(argv)
    manifest_path = args.output / "manifest.json"
    if manifest_path.exists() and not args.overwrite:
        print(f"error: manifest already exists: {manifest_path}", file=sys.stderr)
        return 2

    try:
        importer = SldprtImporter(
            accept_assemblies=not args.no_assemblies,
            include_preview=not args.no_preview,
            limit_profile=args.limits,
        )
        result = importer.import_file(
            args.source,
            options=ImportOptions(
                strict=args.strict,
                include_visualization_mesh=not args.no_mesh,
                include_brep=False,
                mesh_include_edges=not args.no_edges,
            ),
            resources=DirectoryResourceWriter(args.output, overwrite=args.overwrite),
        )
        dump_manifest(result.package, manifest_path, pretty=True)
    except (SldprtImportError, OSError, ValueError) as exc:
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
    statistics = result.statistics
    print(
        f"wrote {manifest_path}: {statistics.get('triangles', 0)} triangles, "
        f"{statistics.get('display_parts', 0)} display parts, "
        f"{warning_count} warnings"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
