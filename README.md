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

The `0.2.0` core provides:

- typed Pydantic models and a generated JSON Schema (IR `0.2.0`);
- graph validation for IDs and cross-object references;
- deterministic manifest JSON serialization;
- atomic, content-addressed directory resource writing;
- optional resource size and SHA-256 validation;
- structured importer diagnostics and a format-adapter protocol;
- package-level `visualizations`: derived, display-only glTF binary assets
  that pack every placed body so a viewer needs no product-graph logic;
- `cad3d_ir.mesh`: a dependency-free glTF binary (GLB) writer, validator and
  triangle helpers shared by adapters;
- a built-in `MeshImporter` for STL (ASCII/binary) and Wavefront OBJ;
- `cad3d-ir validate`, `cad3d-ir schema` and `cad3d-ir import` CLIs.

Kernel-backed importers stay separate. This repository's `cad3d-ir-step`
workspace member reads STEP (AP203/AP214/AP242) and IGES through Open CASCADE
XDE and can emit the same visualization meshes.

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

## Development

```bash
uv sync --dev
uv run pytest
uv run ruff check .
uv run python scripts/sync_schema.py --check

# STEP adapter
uv run --package cad3d-ir-step pytest packages/cad3d-ir-step/tests
uv run --package cad3d-ir-step cad3d-ir-step model.step output/
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

MIT
