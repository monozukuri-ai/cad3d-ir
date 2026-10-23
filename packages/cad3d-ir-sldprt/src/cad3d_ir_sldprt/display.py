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

import sys
from array import array
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


def _rebased_indices(payload: bytes, base: int) -> bytes:
    """Subtract ``base`` from every ``uint32`` of a little-endian index buffer."""
    if base == 0:
        return payload
    values = array("I")
    values.frombytes(payload)
    if sys.byteorder == "big":
        values.byteswap()
    rebased = array("I", (value - base for value in values))
    if sys.byteorder == "big":
        rebased.byteswap()
    return rebased.tobytes()


def _instance_slices(
    result: Any, parts: list[Any]
) -> tuple[
    int, int, bytes, bytes, list[tuple[int, Any, bytes]], bytes, list[list[int]]
]:
    """Cut one instance's vertices, triangles and outlines out of the shared buffers.

    Returns the vertex base and count, the positions and normals bytes, the
    per-part triangle index bytes (already rebased) with their pick ids, the
    rebased outline index bytes and the outline ranges.
    """
    first = min(part.vertex_start for _, part in parts)
    last = max(part.vertex_start + part.vertex_count for _, part in parts)
    count = last - first
    positions = result.positions[first * 12 : last * 12]
    normals = result.normals[first * 12 : last * 12]
    triangles: list[tuple[int, Any, bytes]] = []
    edge_chunks: list[bytes] = []
    edge_ranges: list[list[int]] = []
    edge_cursor = 0
    for pick_id, part in parts:
        if part.triangle_count > 0:
            start = part.triangle_start * _TRIANGLE_BYTES
            chunk = result.indices[
                start : start + part.triangle_count * _TRIANGLE_BYTES
            ]
            triangles.append((pick_id, part, _rebased_indices(chunk, first)))
        if part.edge_count > 0:
            start = part.edge_start * 8
            chunk = result.edges[start : start + part.edge_count * 8]
            edge_chunks.append(_rebased_indices(chunk, first))
            edge_ranges.append([pick_id, edge_cursor, part.edge_count * 2])
            edge_cursor += part.edge_count * 2
    return (
        first,
        count,
        positions,
        normals,
        triangles,
        b"".join(edge_chunks),
        edge_ranges,
    )


def build_assembly_glb(
    result: Any,
    *,
    name: str,
    root_product_id: str,
    root_body_id: str,
    items: list[tuple[Any, str, str, str]],
    unplaced_parts: list[int],
    include_edges: bool,
    generator: str,
    root_extras: dict[str, Any] | None = None,
) -> DisplayGlb:
    """Write an assembly as one node per placed component instance.

    ``items`` are ``(instance, product_id, occurrence_id, body_id)`` for the
    instances to render; each gets its own vertex set, one TRIANGLES primitive
    per saved colour, an outline primitive and a node carrying the component
    transform. ``unplaced_parts`` are face-table indices that stay in stored
    coordinates under the root.
    """
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
    has_normals = len(result.normals) == len(result.positions)
    materials: dict[ColorRGBA | None, int] = {}
    edge_material: int | None = None
    colors: Counter[ColorRGBA] = Counter()
    uncolored = 0
    rendered_parts = 0

    def material_for(key: ColorRGBA | None, label: str | None) -> int:
        material = materials.get(key)
        if material is None:
            if key is None:
                material = builder.add_material("faces", DEFAULT_FACE_COLOR)
            else:
                material = builder.add_material(
                    label or f"faces {len(materials) + 1}", key
                )
            materials[key] = material
        return material

    def add_group(glb_mesh: int, parts: list[tuple[int, Any]]) -> None:
        nonlocal edge_material, uncolored, rendered_parts
        first, count, positions, normals, triangles, edges, edge_ranges = (
            _instance_slices(result, parts)
        )
        if count <= 0 or not positions:
            return
        vertices = builder.add_vertices(
            positions=positions, normals=normals if has_normals else None
        )
        groups: dict[ColorRGBA | None, list[tuple[int, Any, bytes]]] = {}
        for pick_id, part, chunk in triangles:
            key = color_key(part.color)
            if key is None:
                uncolored += 1
            else:
                colors[key] += 1
            groups.setdefault(key, []).append((pick_id, part, chunk))
            rendered_parts += 1
        for key, members in groups.items():
            material = material_for(key, members[0][1].appearance_name)
            ranges: list[list[int]] = []
            cursor = 0
            for pick_id, part, _chunk in members:
                ranges.append([pick_id, cursor, part.triangle_count * 3])
                cursor += part.triangle_count * 3
            builder.add_triangles(
                glb_mesh,
                vertices=vertices,
                indices=b"".join(chunk for _, _, chunk in members),
                material=material,
                extras={"entityKind": "face", "ranges": ranges},
            )
        if include_edges and edges:
            if edge_material is None:
                edge_material = builder.add_material("edges", EDGE_COLOR, unlit=True)
            builder.add_lines(
                glb_mesh,
                vertices=vertices,
                indices=edges,
                material=edge_material,
                extras={"entityKind": "edge", "ranges": edge_ranges},
            )

    material_for(None, None)
    for instance, product_id, occurrence_id, body_id in items:
        parts = [(index + 1, mesh.parts[index]) for index in instance.part_indices]
        if not parts:
            continue
        glb_mesh = builder.add_mesh(
            instance.label, extras={"id": body_id, "product": product_id}
        )
        add_group(glb_mesh, parts)
        builder.add_node(
            instance.label,
            mesh=glb_mesh,
            matrix=instance.matrix if instance.placed else None,
            extras={"id": occurrence_id, "product": product_id},
        )
    if unplaced_parts:
        parts = [(index + 1, mesh.parts[index]) for index in unplaced_parts]
        glb_mesh = builder.add_mesh(
            f"{name} (unplaced)",
            extras={"id": root_body_id, "product": root_product_id},
        )
        add_group(glb_mesh, parts)
        builder.add_node(
            f"{name} (unplaced)",
            mesh=glb_mesh,
            extras={"id": root_product_id, "product": root_product_id},
        )
    payload = builder.build()
    return DisplayGlb(
        payload=payload,
        triangle_count=builder.triangle_count,
        line_segment_count=builder.line_segment_count,
        vertex_count=builder.vertex_count,
        node_count=builder.node_count,
        mesh_count=builder.mesh_count,
        part_count=rendered_parts,
        bounds=builder.bounds,
        colors=colors,
        uncolored_parts=uncolored,
    )


__all__ = [
    "DEFAULT_FACE_COLOR",
    "EDGE_COLOR",
    "DisplayGlb",
    "build_assembly_glb",
    "build_display_glb",
    "color_key",
]
