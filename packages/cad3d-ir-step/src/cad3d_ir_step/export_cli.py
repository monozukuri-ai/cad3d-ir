"""Command-line STEP writer: ``cad3d-ir-step-export``."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from cad3d_ir.mesh import GlbReadError

from cad3d_ir_step.errors import MeshTooLargeError, StepExportError
from cad3d_ir_step.export import (
    DEFAULT_MAX_TRIANGLES,
    STEP_SCHEMAS,
    STEP_UNITS,
    export_step,
    mesh_to_step,
)

_GLB_SUFFIXES = frozenset({".glb"})


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cad3d-ir-step-export",
        description=(
            "Write a STEP file: re-export a STEP or IGES document in another schema "
            "or unit, or build a faceted STEP from the triangles of a cad3d-ir GLB."
        ),
    )
    parser.add_argument(
        "source", type=Path, help="input .step/.stp, .igs/.iges or .glb"
    )
    parser.add_argument("output", type=Path, help="output .step file")
    parser.add_argument(
        "--schema",
        choices=STEP_SCHEMAS,
        default="AP214",
        help="STEP application protocol (default AP214; tessellated meshes use AP242)",
    )
    parser.add_argument(
        "--unit",
        choices=STEP_UNITS,
        default="mm",
        help="length unit written for STEP/IGES sources (default mm)",
    )
    parser.add_argument(
        "--no-colors",
        action="store_true",
        help="do not copy definition colours onto components before writing",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="read the written STEP back and report lost colours",
    )
    parser.add_argument(
        "--mode",
        choices=("solid", "tessellated"),
        default="solid",
        help="GLB sources: sewn solids (default) or AP242 triangulated faces",
    )
    parser.add_argument(
        "--max-triangles",
        type=int,
        default=DEFAULT_MAX_TRIANGLES,
        help=f"GLB sources: refuse meshes above this many triangles (default {DEFAULT_MAX_TRIANGLES}; 0 for no cap)",  # noqa: E501
    )
    parser.add_argument(
        "--name", help="GLB sources: name of the single body or assembly root"
    )
    parser.add_argument(
        "--diagnostics",
        type=Path,
        help="write the diagnostics and counts as JSON",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the STEP writer CLI."""
    args = _parser().parse_args(argv)
    try:
        if args.source.suffix.lower() in _GLB_SUFFIXES:
            result = mesh_to_step(
                args.source.read_bytes(),
                args.output,
                schema=args.schema,
                mode=args.mode,
                max_triangles=args.max_triangles or None,
                name=args.name,
            )
            summary = (
                f"wrote {args.output}: {result.bodies} bodies, "
                f"{result.triangles} triangles, {result.solids} solids, "
                f"{result.open_shells} open shells, {result.byte_length} bytes "
                f"({result.schema}, {result.mode})"
            )
            counts = {
                "bodies": result.bodies,
                "triangles": result.triangles,
                "vertices": result.vertices,
                "degenerate_triangles": result.degenerate_triangles,
                "solids": result.solids,
                "open_shells": result.open_shells,
                "free_edges": result.free_edges,
                "volume_mm3": result.volume_mm3,
                "byte_length": result.byte_length,
                "schema": result.schema,
                "mode": result.mode,
            }
        else:
            result = export_step(
                args.source,
                args.output,
                schema=args.schema,
                unit=args.unit,
                keep_colors=not args.no_colors,
                verify=args.verify,
            )
            summary = (
                f"wrote {args.output}: {result.products} products, {result.components} "
                f"components, {result.colored_shapes} coloured shapes "
                f"({result.colors_copied} colours copied), {result.byte_length} bytes "
                f"({result.schema}, {result.unit})"
            )
            counts = {
                "simple_shapes": result.simple_shapes,
                "assemblies": result.assemblies,
                "components": result.components,
                "colored_shapes": result.colored_shapes,
                "colors_copied": result.colors_copied,
                "colored_shapes_after": result.colored_shapes_after,
                "source_length_units": list(result.source_length_units),
                "byte_length": result.byte_length,
                "schema": result.schema,
                "unit": result.unit,
                "source_format": result.source_format,
            }
    except MeshTooLargeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    except (StepExportError, GlbReadError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.diagnostics is not None:
        args.diagnostics.parent.mkdir(parents=True, exist_ok=True)
        args.diagnostics.write_text(
            json.dumps(
                {
                    "diagnostics": [item.as_dict() for item in result.diagnostics],
                    "statistics": counts,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
    warnings = sum(item.severity in {"warning", "error"} for item in result.diagnostics)
    print(f"{summary}, {warnings} warnings")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
