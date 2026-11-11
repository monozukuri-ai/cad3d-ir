"""OCCT helpers shared with the other exact B-Rep adapters.

They live in :mod:`cad3d_ir_step.shapes` since cad3d-ir-step 0.4.3; this module
keeps the names importable from here.
"""

from cad3d_ir_step.shapes import (
    EDGE_COLOR,
    FACE_COLOR,
    IDENTITY,
    Bounds,
    GlbNode,
    GlbResult,
    Matrix4,
    Rgba,
    brep_bytes,
    build_glb,
    diagonal,
    face_pick_ids,
    location_from_matrix,
    matmul,
    merge_bounds,
    shape_bounds,
    transform_bounds,
    transform_point,
)

__all__ = [
    "EDGE_COLOR",
    "FACE_COLOR",
    "IDENTITY",
    "Bounds",
    "GlbNode",
    "GlbResult",
    "Matrix4",
    "Rgba",
    "brep_bytes",
    "build_glb",
    "diagonal",
    "face_pick_ids",
    "location_from_matrix",
    "matmul",
    "merge_bounds",
    "shape_bounds",
    "transform_bounds",
    "transform_point",
]
