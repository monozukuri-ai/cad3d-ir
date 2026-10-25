"""Wavefront OBJ writer from a visualization GLB.

Each node that places a mesh becomes one ``o`` group; a vertex set shared by
several primitives of that mesh is written once (``v``, optional ``vn``) and
its triangles follow as ``f a//a b//b c//c`` records. Coordinates are package
units (millimetres), stated in a leading comment because OBJ carries no unit.
Edge polylines (LINES / LINE_STRIP primitives) are written as ``l`` records
only on request, so the default output is a clean surface mesh. Materials are
not written: there is no MTL, and viewers show their default appearance.
"""

from __future__ import annotations

from cad3d_ir.constants import PACKAGE_VERSION
from cad3d_ir.exporters.common import (
    OBJ_MEDIA_TYPE,
    MeshExport,
    MeshSource,
    coerce_scene,
    format_number,
    safe_name,
    triangle_normal,
)
from cad3d_ir.mesh.geometry import Bounds, extend_bounds
from cad3d_ir.mesh.scene import MODE_LINES


def write_obj(
    source: MeshSource,
    *,
    normals: bool = True,
    groups: bool = True,
    lines: bool = False,
    precision: int = 7,
    normal_precision: int = 5,
    comment: str | None = None,
) -> MeshExport:
    """Write ``source`` as OBJ text and return the bytes with counts.

    ``normals`` writes ``vn`` records where the GLB carries normals;
    ``groups`` emits one ``o`` record per placed node; ``lines`` adds edge
    polylines as ``l`` records.
    """
    scene = coerce_scene(source)
    out: list[str] = [
        f"# cad3d-ir {PACKAGE_VERSION} Wavefront OBJ",
        "# units: millimetres",
    ]
    if comment:
        out.extend(f"# {line}" for line in comment.splitlines())
    vertex_offset = 0
    normal_offset = 0
    triangles = 0
    vertices = 0
    segments = 0
    dropped = 0
    bounds: Bounds | None = None
    current_node: int | None = None
    used_names: dict[str, int] = {}
    for placed in scene.placed_vertex_sets(lines=lines):
        node = placed.node
        if groups and node.index != current_node:
            current_node = node.index
            base = safe_name(node.name, f"node-{node.index}")
            seen = used_names.get(base, 0)
            used_names[base] = seen + 1
            out.append(f"o {base}" if seen == 0 else f"o {base}_{seen + 1}")
        positions = placed.positions
        count = len(positions) // 3
        bounds = extend_bounds(bounds, positions)
        values = iter(positions)
        out.extend(
            f"v {format_number(x, precision)} {format_number(y, precision)} "
            f"{format_number(z, precision)}"
            for x, y, z in zip(values, values, values, strict=True)
        )
        with_normals = normals and placed.normals is not None
        if with_normals:
            assert placed.normals is not None
            components = iter(placed.normals)
            out.extend(
                f"vn {format_number(x, normal_precision)} "
                f"{format_number(y, normal_precision)} "
                f"{format_number(z, normal_precision)}"
                for x, y, z in zip(components, components, components, strict=True)
            )
        for primitive in placed.triangles:
            indices = primitive.indices
            for k in range(0, len(indices), 3):
                i, j, m = indices[k], indices[k + 1], indices[k + 2]
                a, b, c = i * 3, j * 3, m * 3
                if (
                    triangle_normal(
                        positions[a],
                        positions[a + 1],
                        positions[a + 2],
                        positions[b],
                        positions[b + 1],
                        positions[b + 2],
                        positions[c],
                        positions[c + 1],
                        positions[c + 2],
                    )
                    is None
                ):
                    dropped += 1
                    continue
                va, vb, vc = (
                    vertex_offset + i + 1,
                    vertex_offset + j + 1,
                    vertex_offset + m + 1,
                )
                if with_normals:
                    na, nb, nc = (
                        normal_offset + i + 1,
                        normal_offset + j + 1,
                        normal_offset + m + 1,
                    )
                    out.append(f"f {va}//{na} {vb}//{nb} {vc}//{nc}")
                else:
                    out.append(f"f {va} {vb} {vc}")
                triangles += 1
        for primitive in placed.lines:
            indices = primitive.indices
            if primitive.mode == MODE_LINES:
                for k in range(0, len(indices), 2):
                    out.append(
                        f"l {vertex_offset + indices[k] + 1} "
                        f"{vertex_offset + indices[k + 1] + 1}"
                    )
                segments += len(indices) // 2
            elif len(indices) >= 2:
                out.append("l " + " ".join(str(vertex_offset + v + 1) for v in indices))
                segments += len(indices) - 1
        vertex_offset += count
        vertices += count
        if with_normals:
            normal_offset += count
    data = ("\n".join(out) + "\n").encode("utf-8")
    return MeshExport(
        format="obj",
        data=data,
        media_type=OBJ_MEDIA_TYPE,
        triangles=triangles,
        vertices=vertices,
        degenerate_triangles=dropped,
        line_segments=segments,
        placements=len(used_names) if groups else scene.placement_count,
        bounds=bounds,
    )
