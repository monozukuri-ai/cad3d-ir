# cad3d-ir-icad

`cad3d-ir-icad` imports iCAD SX models (`.icd`, parts and assemblies in one
file) into `cad3d-ir` through
[icadkit](https://github.com/monozukuri-ai/icadkit). It takes the final
bodies iCAD SX saves with each part (embedded Parasolid resources that icadkit
converts into exact OCCT solids through parasolid-kit), places them by the
saved part frames and writes one glTF binary with the same conventions as the
STEP adapter. Native primitives and V7L7 CSG results stand in when a part
stores no saved body. Because the solids are exact B-Rep, the same package can
be written as a STEP assembly (`export_step`).

What the adapter reports but does not map: colours (iCAD stores palette
indices), the 2D drawing views stored in the same file, and parts that are
external references to other `.icd` files. Versions icadkit has not qualified
(for example V8L4 and later) fail closed with `ICD_VERSION_UNQUALIFIED`.

The distribution depends on `cad3d-ir`, `cad3d-ir-step` (tessellation and the
STEP writer) and `icadkit[preview]`; the core distribution does not depend on
this adapter.

## Usage

```python
from pathlib import Path

from cad3d_ir import DirectoryResourceWriter
from cad3d_ir.importers import ImportOptions
from cad3d_ir_icad import IcadImporter, export_step

result = IcadImporter().import_file(
    Path("assembly.icd"),
    options=ImportOptions(include_visualization_mesh=True, include_brep=False),
    resources=DirectoryResourceWriter("converted"),
)
print(result.statistics["parts_converted"], result.statistics["triangles"])
for item in result.diagnostics:
    print(item.code, item.message)

export_step(Path("assembly.icd"), Path("assembly.step"), schema="AP214")
```

```sh
cad3d-ir-icad assembly.icd converted --step assembly.step
```

## Package layout

- One `ProductDefinition` and `Configuration` per saved part record (the
  document root included); one `Occurrence` per child record with the saved
  local transform, so the product tree follows the iCAD part tree.
- One `Body` per part that converted, with a `brep` representation when
  `include_brep` is set (one OCCT BREP resource per distinct solid).
- One package `Visualization` (GLB, millimetres, Z up): one glTF node per part
  record, parts with identical solids share one mesh.

## Diagnostics

| Code | Meaning |
| --- | --- |
| `ICD_SAVED_BREP` | Geometry comes from the saved solids (info) |
| `ICD_APPEARANCE_NOT_MAPPED` | Palette colours are not mapped; default colour (info) |
| `ICD_2D_VIEWS_PRESENT` | The file also stores 2D drawing views (info) |
| `ICD_BODY_UNSUPPORTED` | Parts with saved bodies that did not convert, with reasons |
| `ICD_EXTERNAL_PART_MISSING` | Parts referencing other `.icd` files that were not provided |
| `ICD_PLACEMENT_UNQUALIFIED` | Parts without a qualified frame, placed at their parent's origin |
| `ICD_PART_ORPHAN` | Part records not linked to the root, placed as roots |
| `ICD_LEGACY_VERSION_NO_GEOMETRY` | A version whose native geometry icadkit does not evaluate |
| `ICD_REGISTERED_PART` | A registered 2D library part; no 3D model |
| `ICD_MESH_FAILED` | Solids that could not be tessellated |
| `ICD_SHAPE_LIMIT` | icadkit stopped at a resource limit |
| `ICD_KERNEL_MISSING` | The preview extra (parasolid-kit[occt]) is not installed |
| `ICD_NO_GEOMETRY` | No part yielded a displayable solid |
| `ICD_VERSION_UNQUALIFIED` | The saved version is not qualified by icadkit (error) |

`statistics` carries the saved version (`icd_version`), part and body counts
by outcome, the number of shared meshes, triangle counts and the 2D view
count, so a caller can show what was and was not read.

## License

PolyForm Noncommercial 1.0.0, following icadkit. Commercial licences are
available from UnRobotics Inc.
