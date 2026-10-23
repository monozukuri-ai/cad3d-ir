"""Pack sldkit display meshes into one cad3d-ir GLB without expanding buffers.

sldkit hands over the saved tessellation as four packed little-endian buffers
(positions and normals as ``float32`` triples in millimetres, triangle and
face-outline indices as ``uint32``) plus one record per face table ("part").
The GLB keeps that layout: one shared vertex set, one TRIANGLES primitive per
saved colour whose ``extras.ranges`` list ``[pick_id, first_index,
index_count]`` per part, and one LINES primitive for the face outlines. The
pick id is the 1-based position of the part in ``mesh.parts``.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any

from cad3d_ir.mesh import GlbBuilder
from cad3d_ir.mesh.geometry import Bounds

ColorRGBA = tuple[float, float, float, float]

DEFAULT_FACE_COLOR: ColorRGBA = (0.55, 0.62, 0.70, 1.0)
EDGE_COLOR: ColorRGBA = (0.08, 0.10, 0.14, 1.0)
_TRIANGLE_BYTES = 12  # three uint32 indices


@dataclass(slots=True)
class DisplayGlb:
    """The packed GLB of one display mesh and the counts a manifest needs."""

    payload: bytes
    triangle_count: int
    line_segment_count: int
    vertex_count: int
    node_count: int
    mesh_count: int
    part_count: int
    bounds: Bounds | None
    colors: Counter[ColorRGBA]
    uncolored_parts: int

    @property
    def dominant_color(self) -> ColorRGBA | None:
        """The saved colour used by the most parts, if any part has one."""
        if not self.colors:
            return None
        return self.colors.most_common(1)[0][0]


def color_key(color: Any) -> ColorRGBA | None:
    """Normalize a sldkit part colour to a hashable RGBA tuple in ``[0, 1]``."""
    if color is None:
        return None
    channels = tuple(min(1.0, max(0.0, float(channel))) for channel in color)
    if len(channels) == 3:
        channels = (*channels, 1.0)
    if len(channels) != 4:
        return None
    return channels  # type: ignore[return-value]


def build_display_glb(
    result: Any,
    *,
    name: str,
    body_id: str,
    product_id: str,
    include_edges: bool,
    generator: str,
    root_extras: dict[str, Any] | None = None,
) -> DisplayGlb:
    """Write a :class:`sldkit.DisplayMeshResult` as one single-body GLB."""
    mesh = result.mesh
    if mesh is None or not result.positions:
        raise ValueError("display result carries no mesh")
    builder = GlbBuilder(
        generator=generator,
        root_name=name,
        length_unit_to_m=0.001,
        up_axis="z",
        root_extras=root_extras,
    )
    normals = result.normals if len(result.normals) == len(result.positions) else None
    vertices = builder.add_vertices(positions=result.positions, normals=normals)
    glb_mesh = builder.add_mesh(name, extras={"id": body_id, "product": product_id})

    groups: dict[ColorRGBA | None, list[tuple[int, Any]]] = {}
    colors: Counter[ColorRGBA] = Counter()
    uncolored = 0
    for pick_id, part in enumerate(mesh.parts, start=1):
        key = color_key(part.color)
        if key is None:
            uncolored += 1
        else:
            colors[key] += 1
        if part.triangle_count > 0:
            groups.setdefault(key, []).append((pick_id, part))

    materials: dict[ColorRGBA | None, int] = {}
    for key, members in groups.items():
        material = materials.get(key)
        if material is None:
            if key is None:
                material = builder.add_material("faces", DEFAULT_FACE_COLOR)
            else:
                label = members[0][1].appearance_name or f"faces {len(materials) + 1}"
                material = builder.add_material(str(label), key)
            materials[key] = material
        chunks: list[bytes] = []
        ranges: list[list[int]] = []
        cursor = 0
        for pick_id, part in members:
            start = part.triangle_start * _TRIANGLE_BYTES
            chunks.append(result.indices[start : start + part.triangle_count * 12])
            ranges.append([pick_id, cursor, part.triangle_count * 3])
            cursor += part.triangle_count * 3
        builder.add_triangles(
            glb_mesh,
            vertices=vertices,
            indices=b"".join(chunks),
            material=material,
            extras={"entityKind": "face", "ranges": ranges},
        )

    if include_edges and result.edges and mesh.edge_count > 0:
        edge_material = builder.add_material("edges", EDGE_COLOR, unlit=True)
        edge_ranges = [
            [pick_id, part.edge_start * 2, part.edge_count * 2]
            for pick_id, part in enumerate(mesh.parts, start=1)
            if part.edge_count > 0
        ]
        builder.add_lines(
            glb_mesh,
            vertices=vertices,
            indices=result.edges,
            material=edge_material,
            extras={"entityKind": "edge", "ranges": edge_ranges},
        )

    builder.add_node(
        name, mesh=glb_mesh, extras={"id": product_id, "product": product_id}
    )
    payload = builder.build()
    return DisplayGlb(
        payload=payload,
        triangle_count=builder.triangle_count,
        line_segment_count=builder.line_segment_count,
        vertex_count=builder.vertex_count,
        node_count=builder.node_count,
        mesh_count=builder.mesh_count,
        part_count=len(mesh.parts),
        bounds=builder.bounds,
        colors=colors,
        uncolored_parts=uncolored,
    )


__all__ = [
    "DEFAULT_FACE_COLOR",
    "EDGE_COLOR",
    "DisplayGlb",
    "build_display_glb",
    "color_key",
]
