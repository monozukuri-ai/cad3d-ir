"""Command-line conversion entry point for cad3d-ir-edrawings."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from cad3d_ir import DirectoryResourceWriter, dump_manifest
from cad3d_ir.importers import ImportOptions

from cad3d_ir_edrawings import EdrawingsImporter, EdrawingsImportError
from cad3d_ir_edrawings.importer import LIMIT_PROFILES


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cad3d-ir-edrawings",
        description=(
            "Convert an eDrawings part or assembly (.eprt/.easm) to a cad3d-ir "
            "package directory with a glTF binary of its saved display meshes."
        ),
    )
    parser.add_argument("source", type=Path, help="input .eprt or .easm file")
    parser.add_argument("output", type=Path, help="output package directory")
    parser.add_argument(
        "--no-mesh",
        action="store_true",
        help="write metadata and the preview image only, without the display GLB",
    )
    parser.add_argument(
        "--no-edges",
        action="store_true",
        help="leave the outline LINES primitives out of the GLB",
    )
    parser.add_argument(
        "--no-preview",
        action="store_true",
        help="do not write the saved preview image",
    )
    parser.add_argument(
        "--accept-drawings",
        action="store_true",
        help="read the 3D bodies of .edrw drawings instead of rejecting them",
    )
    parser.add_argument(
        "--configuration",
        help="saved configuration to show (its name or configN segment)",
    )
    parser.add_argument(
        "--display-state",
        help="saved display state of the shown configuration (dispN or its name)",
    )
    parser.add_argument(
        "--include-hidden",
        action="store_true",
        help="keep components eDrawings hides as invisible occurrences",
    )
    parser.add_argument(
        "--limits",
        default="service",
        choices=LIMIT_PROFILES,
        help="edrawings-kit limit profile (default: service)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="fail when the document has neither 3D bodies nor a preview image",
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
    """Run the eDrawings adapter CLI."""
    args = _parser().parse_args(argv)
    manifest_path = args.output / "manifest.json"
    if manifest_path.exists() and not args.overwrite:
        print(f"error: manifest already exists: {manifest_path}", file=sys.stderr)
        return 2

    try:
        importer = EdrawingsImporter(
            accept_drawings=args.accept_drawings,
            include_preview=not args.no_preview,
            configuration=args.configuration,
            display_state=args.display_state,
            include_hidden=args.include_hidden,
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
    except (EdrawingsImportError, OSError, ValueError) as exc:
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
        f"{statistics.get('occurrences', 0)} placements of "
        f"{statistics.get('parts', 0)} parts, {warning_count} warnings"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
