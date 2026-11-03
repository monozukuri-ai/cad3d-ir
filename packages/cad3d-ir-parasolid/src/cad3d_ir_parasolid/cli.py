"""Command-line conversion entry point for cad3d-ir-parasolid."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from cad3d_ir import DirectoryResourceWriter, dump_manifest
from cad3d_ir.importers import ImportOptions

from cad3d_ir_parasolid import ParasolidImporter, ParasolidImportError, export_step
from cad3d_ir_parasolid.export import STEP_SCHEMAS


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cad3d-ir-parasolid",
        description=(
            "Convert a Parasolid transmit file (.x_t, .x_b, .xmt_txt, .xmt_bin) to a "
            "cad3d-ir package directory with a glTF binary of its bodies, and "
            "optionally to a STEP file."
        ),
    )
    parser.add_argument("source", type=Path, help="input X_T / X_B file")
    parser.add_argument("output", type=Path, help="output package directory")
    parser.add_argument(
        "--no-mesh", action="store_true", help="write the manifest without the GLB"
    )
    parser.add_argument(
        "--no-edges",
        action="store_true",
        help="leave the face-outline LINES primitive out of the GLB",
    )
    parser.add_argument(
        "--brep",
        action="store_true",
        help="also write each converted body as an OCCT BREP resource",
    )
    parser.add_argument(
        "--no-partial",
        action="store_true",
        help="fail a body whose faces cannot all be built instead of dropping them",
    )
    parser.add_argument(
        "--step", type=Path, help="also write the converted bodies as this STEP file"
    )
    parser.add_argument(
        "--schema",
        default="AP214",
        choices=STEP_SCHEMAS,
        help="STEP schema for --step (default: AP214)",
    )
    parser.add_argument(
        "--strict", action="store_true", help="fail when no body can be displayed"
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="replace existing manifest and resources",
    )
    parser.add_argument(
        "--diagnostics", type=Path, help="write structured import diagnostics as JSON"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the Parasolid adapter CLI."""
    args = _parser().parse_args(argv)
    manifest_path = args.output / "manifest.json"
    if manifest_path.exists() and not args.overwrite:
        print(f"error: manifest already exists: {manifest_path}", file=sys.stderr)
        return 2
    try:
        result = ParasolidImporter(partial=not args.no_partial).import_file(
            args.source,
            options=ImportOptions(
                strict=args.strict,
                include_visualization_mesh=not args.no_mesh,
                include_brep=args.brep,
                mesh_include_edges=not args.no_edges,
            ),
            resources=DirectoryResourceWriter(args.output, overwrite=args.overwrite),
        )
        dump_manifest(result.package, manifest_path, pretty=True)
        if args.step is not None:
            step = export_step(args.source, args.step, schema=args.schema)
            print(
                f"wrote {step.path}: {step.placed} placed bodies, "
                f"{len(step.skipped)} skipped"
            )
    except (ParasolidImportError, OSError, ValueError) as exc:
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
        f"{statistics.get('bodies_converted', 0)} of {statistics.get('bodies', 0)} "
        f"bodies converted, {warning_count} warnings"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
