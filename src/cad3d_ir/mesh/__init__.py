"""Dependency-free mesh helpers: triangle utilities, a glTF binary writer and reader."""

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
from cad3d_ir.mesh.scene import (
    GlbReadError,
    MeshScene,
    PlacedVertexSet,
    SceneMesh,
    SceneNode,
    ScenePrimitive,
    read_glb,
)

__all__ = [
    "GLB_ENCODING",
    "GLB_MEDIA_TYPE",
    "GlbBuilder",
    "GlbReadError",
    "GlbValidationReport",
    "MeshScene",
    "PlacedVertexSet",
    "SceneMesh",
    "SceneNode",
    "ScenePrimitive",
    "drop_degenerate_triangles",
    "extend_bounds",
    "flat_normals",
    "read_glb",
    "validate_glb_bytes",
    "vertex_normals",
]
