# cad3d-ir

`cad3d-ir` is a typed, kernel-neutral intermediate representation for 3D CAD
product data.

The core package represents source documents, reusable product definitions,
configurations, component occurrences, bodies, geometry-resource references,
materials, appearances, provenance, and namespaced source extensions. Exact
B-Rep and mesh bytes remain external resources rather than Python or JSON
objects embedded in the manifest.

The core intentionally does not depend on Open CASCADE, CadQuery, a vendor SDK,
or a viewer. Format parsers and geometry backends integrate through separate
adapter packages.

## Status

The `0.3.0` core provides:

- typed Pydantic models and a generated JSON Schema (IR `0.2.0`);
- graph validation for IDs and cross-object references;
- deterministic manifest JSON serialization;
- atomic, content-addressed directory resource writing;
- optional resource size and SHA-256 validation;
- structured importer diagnostics and a format-adapter protocol;
- package-level `visualizations`: derived, display-only glTF binary assets
  that pack every placed body so a viewer needs no product-graph logic;
- `cad3d_ir.mesh`: a dependency-free glTF binary (GLB) writer, validator and
  triangle helpers shared by adapters. The writer takes flat sequences or
  packed little-endian buffers and can share one vertex set between
  primitives;
- `cad3d_ir.mesh.read_glb`: reads those GLBs back into nodes, placements and
  primitives in package coordinates (the inverse of the writer);
- `cad3d_ir.exporters`: STL (binary or ASCII) and Wavefront OBJ writers that
  take a visualization GLB and write every placed triangle in package units,
  plus output-size estimates;
- a built-in `MeshImporter` for STL (ASCII/binary) and Wavefront OBJ;
- `cad3d-ir validate`, `cad3d-ir schema`, `cad3d-ir import` and
  `cad3d-ir export` CLIs.

Kernel-backed and native-format importers stay separate. This repository's
`cad3d-ir-step` workspace member reads STEP (AP203/AP214/AP242) and IGES
through Open CASCADE XDE, can emit the same visualization meshes, and writes
STEP (re-export in another schema or unit, or faceted solids from a GLB). The
`cad3d-ir-sldprt` member reads SOLIDWORKS parts through
[sldkit](https://github.com/monozukuri-ai/sldkit): it packs the display
meshes SOLIDWORKS saved into the same GLB layout, without reading the B-Rep,
and carries the document metadata and preview image. It is licensed
separately (PolyForm Noncommercial, see its README). The `cad3d-ir-icad`
member reads iCAD SX models (`.icd`, parts and assemblies in one file)
through [icadkit](https://github.com/monozukuri-ai/icadkit): the final
bodies iCAD saved with each part become exact OCCT solids placed by the saved
part frames, written as the same GLB layout and, through `export_step`, as a
STEP assembly. It follows icadkit's licensing (PolyForm Noncommercial).

## Visualization meshes

Adapters write display geometry on request:

```python
from pathlib import Path

from cad3d_ir import DirectoryResourceWriter
from cad3d_ir.importers import ImportOptions, MeshImporter

result = MeshImporter().import_file(
    Path("part.stl"),
    options=ImportOptions(),
    resources=DirectoryResourceWriter("converted"),
)
preview = result.package.visualizations[0]  # kind="glb", scope="package"
print(preview.statistics["triangles"], preview.resource_id)
```

The GLB is self-contained: positions, optional normals and `uint32` indices in
one buffer. Node `0` carries the single conversion from package units and Z-up
to glTF metres and Y-up (`extras.cad3d_ir.length_unit_to_m`), so every other
node and mesh stays in package coordinates. Each node's `extras.id` is the IR
occurrence (or root product) id, each mesh's `extras.id` the body id, and
face/edge primitives list `[pick_id, first_index, index_count]` ranges in
`extras.ranges`, so viewers can map a picked triangle back to a face.

## Exporting meshes

Any visualization GLB can be written back out as STL or OBJ. The exporters walk
the node tree, apply every placement and write the triangles in package units
(millimetres), so an assembly comes out as one placed mesh. Zero-area triangles
are dropped and counted; the estimates let a caller refuse an output that would
be too large before doing the work.

```python
from pathlib import Path

from cad3d_ir.exporters import estimate_obj_bytes, write_obj, write_stl
from cad3d_ir.mesh import read_glb

scene = read_glb(Path("converted/geometry/part-abc123.glb").read_bytes())
stl = write_stl(scene)  # binary, face normals
Path("part.stl").write_bytes(stl.data)
if estimate_obj_bytes(scene.placed_vertex_count, scene.placed_triangle_count) < 200e6:
    obj = write_obj(scene)  # one ``o`` per node, ``v`` / ``vn`` / ``f``, no MTL
    Path("part.obj").write_bytes(obj.data)
print(stl.triangles, stl.degenerate_triangles, stl.bounds)
```

`cad3d-ir export part.glb --stl part.stl --obj part.obj` does the same from the
command line (`--ascii-stl`, `--no-normals`, `--no-groups`, `--lines` for edge
polylines). Both writers accept the GLB bytes directly in place of a scene.

## Development

```bash
uv sync --dev
uv run pytest
uv run ruff check .
uv run python scripts/sync_schema.py --check

# STEP adapter
uv run --package cad3d-ir-step pytest packages/cad3d-ir-step/tests
uv run --package cad3d-ir-step cad3d-ir-step model.step output/

# SOLIDWORKS part adapter
uv run --package cad3d-ir-sldprt pytest packages/cad3d-ir-sldprt/tests
uv run --package cad3d-ir-sldprt cad3d-ir-sldprt part.SLDPRT output/

# iCAD SX adapter
uv run --package cad3d-ir-icad pytest packages/cad3d-ir-icad/tests
uv run --package cad3d-ir-icad cad3d-ir-icad model.icd output/ --step model.step
```

## Minimal use

```python
from cad3d_ir import (
    CadPackage,
    CoordinateSystem,
    UnitSystem,
    dump_manifest,
    load_manifest,
)

package = CadPackage(
    format="cad3d-ir",
    version="0.1.0",
    id="package-example",
    units=UnitSystem(length_unit="mm"),
    coordinate_system=CoordinateSystem(),
)

dump_manifest(package, "manifest.json", pretty=True)
loaded = load_manifest("manifest.json")
assert loaded == package
```

See [the design document](docs/design.md) for the package boundary, model
invariants, geometry-resource strategy, and staged scope. The reference adapter
and abstraction gate are described in
[the adapter design note](docs/reference-adapters.md).

## License

`cad3d-ir` and `cad3d-ir-step` are MIT licensed. `packages/cad3d-ir-sldprt`
and `packages/cad3d-ir-icad` are licensed separately under the PolyForm
Noncommercial License 1.0.0 by UnRobotics Inc., following sldkit and icadkit;
see [the SOLIDWORKS adapter's LICENSE](packages/cad3d-ir-sldprt/LICENSE) and
[the iCAD adapter's LICENSE](packages/cad3d-ir-icad/LICENSE) for the notices
and commercial contact.
