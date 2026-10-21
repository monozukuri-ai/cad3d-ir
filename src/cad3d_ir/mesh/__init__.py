"""Dependency-free mesh helpers: triangle utilities and a glTF binary writer."""

from cad3d_ir.mesh.geometry import (
    drop_degenerate_triangles,
    extend_bounds,
    flat_normals,
    vertex_normals,
)
from cad3d_ir.mesh.glb import (
    GLB_ENCODING,
    GLB_MEDIA_TYPE,
    GlbBuilder,
    GlbValidationReport,
    validate_glb_bytes,
)

__all__ = [
    "GLB_ENCODING",
    "GLB_MEDIA_TYPE",
    "GlbBuilder",
    "GlbValidationReport",
    "drop_degenerate_triangles",
    "extend_bounds",
    "flat_normals",
    "validate_glb_bytes",
    "vertex_normals",
]
