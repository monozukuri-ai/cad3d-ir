"""STL writer (binary by default, ASCII on request) from a visualization GLB.

STL has no units, structure or colour: every placed triangle of every node is
written once, in package units (millimetres), with its face normal. Zero-area
triangles are left out and counted. The 80-byte binary header never starts
with ``solid`` so sniffers do not mistake it for ASCII.
"""

from __future__ import annotations

import struct
from typing import Final

from cad3d_ir.constants import PACKAGE_VERSION
from cad3d_ir.exporters.common import (
    STL_MEDIA_TYPE,
    MeshExport,
    MeshSource,
    coerce_scene,
    format_number,
    safe_name,
    triangle_normal,
)
from cad3d_ir.mesh.geometry import Bounds, extend_bounds

STL_BINARY_HEADER_BYTES: Final = 80 + 4
STL_BINARY_TRIANGLE_BYTES: Final = 50
_RECORD: Final = struct.Struct("<12fH")


def write_stl(
    source: MeshSource,
    *,
    binary: bool = True,
    name: str = "cad3d-ir",
    header: str | None = None,
    precision: int = 7,
) -> MeshExport:
    """Write every placed triangle of ``source`` as STL and return the bytes.

    ``name`` labels the ASCII ``solid``; ``header`` replaces the binary header
    text (truncated to 80 bytes, must not start with ``solid``).
    """
    scene = coerce_scene(source)
    records: list[bytes] = []
    lines: list[str] = []
    label = safe_name(name, "cad3d-ir")
    if not binary:
        lines.append(f"solid {label}")
    pack = _RECORD.pack
    dropped = 0
    bounds: Bounds | None = None
    nodes: set[int] = set()
    for placed in scene.placed_vertex_sets(lines=False):
        positions = placed.positions
        bounds = extend_bounds(bounds, positions)
        nodes.add(placed.node.index)
        for primitive in placed.triangles:
            indices = primitive.indices
            for k in range(0, len(indices), 3):
                a, b, c = indices[k] * 3, indices[k + 1] * 3, indices[k + 2] * 3
                ax, ay, az = positions[a], positions[a + 1], positions[a + 2]
                bx, by, bz = positions[b], positions[b + 1], positions[b + 2]
                cx, cy, cz = positions[c], positions[c + 1], positions[c + 2]
                normal = triangle_normal(ax, ay, az, bx, by, bz, cx, cy, cz)
                if normal is None:
                    dropped += 1
                    continue
                if binary:
                    records.append(pack(*normal, ax, ay, az, bx, by, bz, cx, cy, cz, 0))
                else:
                    nx, ny, nz = (format_number(v, precision) for v in normal)
                    lines.append(f"  facet normal {nx} {ny} {nz}")
                    lines.append("    outer loop")
                    for x, y, z in ((ax, ay, az), (bx, by, bz), (cx, cy, cz)):
                        lines.append(
                            "      vertex "
                            f"{format_number(x, precision)} "
                            f"{format_number(y, precision)} "
                            f"{format_number(z, precision)}"
                        )
                    lines.append("    endloop")
                    lines.append("  endfacet")
    if binary:
        count = len(records)
        text = (
            header
            if header is not None
            else (f"cad3d-ir {PACKAGE_VERSION} binary STL; units: millimetres")
        )
        head = text.encode("utf-8", "replace")[:80]
        if head.lower().startswith(b"solid"):
            raise ValueError("a binary STL header must not start with 'solid'")
        data = head.ljust(80, b"\0") + struct.pack("<I", count) + b"".join(records)
    else:
        count = (len(lines) - 1) // 7
        lines.append(f"endsolid {label}")
        data = ("\n".join(lines) + "\n").encode("utf-8")
    return MeshExport(
        format="stl",
        data=data,
        media_type=STL_MEDIA_TYPE,
        triangles=count,
        vertices=count * 3,
        degenerate_triangles=dropped,
        placements=len(nodes),
        bounds=bounds,
    )
