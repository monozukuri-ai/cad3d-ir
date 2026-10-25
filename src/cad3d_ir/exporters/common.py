"""Shared pieces of the mesh exporters: the result type and small helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Literal

from cad3d_ir.mesh.geometry import Bounds
from cad3d_ir.mesh.scene import MeshScene, read_glb

STL_MEDIA_TYPE: Final = "model/stl"
OBJ_MEDIA_TYPE: Final = "model/obj"

ExportFormat = Literal["stl", "obj"]
MeshSource = MeshScene | bytes | bytearray | memoryview
"""A scene read with :func:`cad3d_ir.mesh.read_glb`, or the GLB bytes themselves."""

_SUFFIXES: Final = {"stl": ".stl", "obj": ".obj"}
_UNSAFE_NAME: Final = re.compile(r"[\s\x00-\x1f\x7f]+")


@dataclass(frozen=True, slots=True)
class MeshExport:
    """The bytes of one exported mesh file and what went into it.

    ``triangles`` and ``vertices`` are what was written (STL is de-indexed, so
    its vertices are three per triangle); ``degenerate_triangles`` were
    zero-area triangles left out. ``placements`` counts the nodes whose meshes
    contributed. Coordinates are package units (millimetres for every adapter
    in this repository).
    """

    format: ExportFormat
    data: bytes
    media_type: str
    triangles: int
    vertices: int
    degenerate_triangles: int = 0
    line_segments: int = 0
    placements: int = 0
    bounds: Bounds | None = None

    @property
    def byte_length(self) -> int:
        return len(self.data)

    @property
    def suffix(self) -> str:
        return _SUFFIXES[self.format]


def coerce_scene(source: MeshSource) -> MeshScene:
    """Return ``source`` as a :class:`MeshScene`, reading GLB bytes if needed."""
    if isinstance(source, MeshScene):
        return source
    if isinstance(source, bytes | bytearray | memoryview):
        return read_glb(bytes(source))
    raise TypeError("source must be a MeshScene or GLB bytes")


def safe_name(value: str | None, fallback: str) -> str:
    """Collapse whitespace and control characters so a name fits on one record."""
    cleaned = _UNSAFE_NAME.sub("_", (value or "").strip()).strip("_")
    return cleaned or fallback


def format_number(value: float, precision: int) -> str:
    """Shortest ``%g`` form with ``precision`` significant digits (``-0`` → ``0``)."""
    text = format(value, f".{precision}g")
    return "0" if text == "-0" else text


def triangle_normal(
    ax: float,
    ay: float,
    az: float,
    bx: float,
    by: float,
    bz: float,
    cx: float,
    cy: float,
    cz: float,
) -> tuple[float, float, float] | None:
    """Unit normal of a triangle, or ``None`` when it has no area."""
    ux, uy, uz = bx - ax, by - ay, bz - az
    vx, vy, vz = cx - ax, cy - ay, cz - az
    nx = uy * vz - uz * vy
    ny = uz * vx - ux * vz
    nz = ux * vy - uy * vx
    if nx == 0.0 and ny == 0.0 and nz == 0.0:
        return None
    length = (nx * nx + ny * ny + nz * nz) ** 0.5
    return nx / length, ny / length, nz / length
