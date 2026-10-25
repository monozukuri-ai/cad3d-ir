"""Output-size estimates, so callers can refuse an export before writing it.

Binary STL is exact. OBJ and ASCII STL are text whose size depends on how
many digits the numbers and indices need; the constants here were measured
against the writers with their default precisions (millimetre coordinates,
``%.7g`` positions, ``%.5g`` normals) and are tested to stay within 15 % of
the real output for meshes of a few thousand triangles and up.
"""

from __future__ import annotations

from typing import Final

from cad3d_ir.exporters.stl import STL_BINARY_HEADER_BYTES, STL_BINARY_TRIANGLE_BYTES

STL_ASCII_TRIANGLE_BYTES: Final = 190
"""Seven text records per triangle with ``%.7g`` numbers."""
OBJ_HEADER_BYTES: Final = 64
OBJ_GROUP_BYTES: Final = 16
OBJ_VERTEX_BYTES: Final = 23
"""``v`` record with three ``%.7g`` millimetre values."""
OBJ_NORMAL_BYTES: Final = 28
"""``vn`` record with three ``%.5g`` components."""


def estimate_stl_bytes(triangles: int, *, binary: bool = True) -> int:
    """Return the STL size for ``triangles`` faces (exact for binary)."""
    if triangles < 0:
        raise ValueError("triangles must not be negative")
    if binary:
        return STL_BINARY_HEADER_BYTES + STL_BINARY_TRIANGLE_BYTES * triangles
    return 32 + STL_ASCII_TRIANGLE_BYTES * triangles


def estimate_obj_bytes(
    vertices: int,
    triangles: int,
    *,
    normals: bool = True,
    groups: int = 1,
) -> int:
    """Return an OBJ size estimate for ``vertices`` and ``triangles``.

    ``vertices`` are the vertices actually written (a mesh placed twice writes
    its vertex set twice; see ``MeshScene.placed_vertex_count``).
    """
    if vertices < 0 or triangles < 0 or groups < 0:
        raise ValueError("counts must not be negative")
    digits = len(str(max(vertices, 1)))
    # "f " + three "<v>//<n>" or "<v>" tokens + newline. Indices below the
    # maximum have fewer digits, so the mean digit count is a little under
    # ``digits``; the offsets were fitted to the writer's output.
    mean_digits = max(digits - 0.1, 1.0)
    token = 2 * mean_digits + 2 if normals else mean_digits
    face_bytes = 4 + 3 * token
    size = OBJ_HEADER_BYTES + OBJ_GROUP_BYTES * groups
    size += OBJ_VERTEX_BYTES * vertices
    if normals:
        size += OBJ_NORMAL_BYTES * vertices
    size += int(face_bytes * triangles)
    return int(size)
