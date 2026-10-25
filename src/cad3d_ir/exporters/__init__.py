"""Mesh exporters: STL and Wavefront OBJ from a visualization GLB.

The visualization GLB an adapter writes is the one place where every body is
tessellated and placed, so it is also the source for mesh exports. Read it
with :func:`cad3d_ir.mesh.read_glb` (or hand the bytes straight to a writer),
and use the estimates to refuse outputs that would be too large before doing
the work.

```python
from cad3d_ir.exporters import write_obj, write_stl

stl = write_stl(glb_bytes)              # binary STL, millimetres
obj = write_obj(glb_bytes, lines=False)  # ``o`` per node, ``v``/``vn``/``f``
print(stl.triangles, stl.degenerate_triangles, len(obj.data))
```
"""

from cad3d_ir.exporters.common import (
    OBJ_MEDIA_TYPE,
    STL_MEDIA_TYPE,
    ExportFormat,
    MeshExport,
    MeshSource,
    coerce_scene,
)
from cad3d_ir.exporters.estimate import (
    OBJ_NORMAL_BYTES,
    OBJ_VERTEX_BYTES,
    STL_ASCII_TRIANGLE_BYTES,
    estimate_obj_bytes,
    estimate_stl_bytes,
)
from cad3d_ir.exporters.obj import write_obj
from cad3d_ir.exporters.stl import (
    STL_BINARY_HEADER_BYTES,
    STL_BINARY_TRIANGLE_BYTES,
    write_stl,
)

__all__ = [
    "OBJ_MEDIA_TYPE",
    "OBJ_NORMAL_BYTES",
    "OBJ_VERTEX_BYTES",
    "STL_ASCII_TRIANGLE_BYTES",
    "STL_BINARY_HEADER_BYTES",
    "STL_BINARY_TRIANGLE_BYTES",
    "STL_MEDIA_TYPE",
    "ExportFormat",
    "MeshExport",
    "MeshSource",
    "coerce_scene",
    "estimate_obj_bytes",
    "estimate_stl_bytes",
    "write_obj",
    "write_stl",
]
