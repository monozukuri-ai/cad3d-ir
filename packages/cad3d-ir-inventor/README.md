# cad3d-ir-inventor

`cad3d-ir-inventor` imports Autodesk Inventor parts (`.ipt`) into `cad3d-ir`
through [inventor-kit](https://github.com/monozukuri-ai/inventor-kit). It
takes the B-Rep table the part stores, converts each body into an exact OCCT
solid (`convert_bodies(backend="occt")`, millimetres) and writes one glTF
binary with the same conventions as the STEP adapter. Because the solids are
exact B-Rep, the same part can be written as a STEP assembly (`export_step`).

What the adapter does not do:

- It reads the stored table as it is. Whether that table is the model state
  Inventor shows (suppressed features, other model states) is not established
  (`INVENTOR_STATE_UNVERIFIED`); nothing is recomputed from the feature
  history.
- Appearances are not read; bodies use the default colour.
- Assemblies (`.iam`), drawings (`.idw`) and presentations (`.ipn`) are
  identified and reported (`INVENTOR_KIND_UNSUPPORTED`), not converted.
- Curves and surfaces Inventor defines by a procedure (intersections, blends,
  offsets) are represented by the B-spline fits it stored with them, within
  the stored fit tolerance (`INVENTOR_APPROXIMATED_GEOMETRY`). Geometry
  without a stored fit, such as helical thread surfaces, leaves its body out
  (`INVENTOR_BODY_UNSUPPORTED`).
- Save generations inventor-kit does not read fail closed
  (`INVENTOR_SEGMENT_MAJOR_UNSUPPORTED`). A generation it frames like its
  verified neighbours is read with a warning (`INVENTOR_PROFILE_CANDIDATE`).

The distribution depends on `cad3d-ir`, `cad3d-ir-step` (tessellation and the
STEP writer) and `inventor-kit[occt]`; the core distribution does not depend on
this adapter. inventor-kit needs Python 3.11 or later: on Python 3.10 the
package installs without it and reading raises `InventorImportError`.

## Usage

```python
from pathlib import Path

from cad3d_ir import DirectoryResourceWriter
from cad3d_ir.importers import ImportOptions
from cad3d_ir_inventor import InventorImporter, export_step

result = InventorImporter().import_file(
    Path("part.ipt"),
    options=ImportOptions(
        strict=False, include_visualization_mesh=True, include_brep=False
    ),
    resources=DirectoryResourceWriter("converted"),
)
print(result.statistics["bodies_converted"], result.statistics["triangles"])
for item in result.diagnostics:
    print(item.code, item.message)

export_step(Path("part.ipt"), Path("part.step"), schema="AP214")
```

```sh
cad3d-ir-inventor part.ipt converted --step part.step
```

`read_source` returns the document and its conversion once, so one read can
feed both `map_source` and `export_step`.

## Package layout

- One `part` `ProductDefinition` for the file with one `Configuration`
  (`Stored state`) that lists every converted body. A part has no
  occurrences.
- One `Body` per converted body, named `Body_<index>` after its place in the
  stored table, with a `brep` representation when `include_brep` is set (one
  OCCT BREP resource per body; `approximate` when stored fits stand in).
- One package `Visualization` (GLB, millimetres, Z up): one glTF node and one
  mesh per body.
- The preview image the document stores, when it has one, as a resource with
  `properties.role == "preview-image"`; `metadata.preview_resource_id` names
  it. It is written even when no body converts, so a caller can show it.
- The source document's properties carry the segment major, the save version,
  the Inventor release derived from the major, and the iProperties
  inventor-kit names (`part_number`, `title`, ...).

## Diagnostics

| Code | Meaning |
| --- | --- |
| `INVENTOR_EXACT_BREP` | Geometry is the B-Rep the part stores, converted to OCCT (info) |
| `INVENTOR_STATE_UNVERIFIED` | The stored table is shown; the current model state is not established (info) |
| `INVENTOR_APPEARANCE_NOT_READ` | Appearances are not read; default colour (info) |
| `INVENTOR_APPROXIMATED_GEOMETRY` | Stored B-spline fits stand in for procedural curves and surfaces (info) |
| `INVENTOR_BODY_SHEET` | Bodies whose faces do not close a volume are left out (info) |
| `INVENTOR_PROFILE_CANDIDATE` | The save generation is read as a candidate |
| `INVENTOR_BODY_UNSUPPORTED` | Bodies with geometry that is not converted, with reasons |
| `INVENTOR_BODY_FAILED` | Bodies that could not be built, with reasons |
| `INVENTOR_MESH_FAILED` | Bodies that could not be tessellated |
| `INVENTOR_PREVIEW_ONLY` | No shape can be displayed; the stored preview image is available |
| `INVENTOR_NO_GEOMETRY` | No body yielded a displayable shape (error) |
| `INVENTOR_GEOMETRY_UNAVAILABLE` | The stored B-Rep could not be extracted (error) |
| `INVENTOR_SEGMENT_MAJOR_UNSUPPORTED` | The save generation is outside those inventor-kit reads (error) |
| `INVENTOR_KIND_UNSUPPORTED` | The document is an assembly, drawing or presentation (error) |
| `INVENTOR_CONVERSION_FAILED` | The bodies could not be converted to OCCT (error) |
| `INVENTOR_KERNEL_MISSING` | inventor-kit's occt extra is not installed (error) |
| `INVENTOR_LIMIT` | inventor-kit stopped at a resource limit (error) |
| `INVENTOR_READ_FAILED` | The document could not be read (error) |

`statistics` carries the segment major, save version and release, whether the
generation is a candidate, body counts by outcome (`bodies_converted`,
`bodies_open_shell`, `bodies_unsupported`, `bodies_failed`), the reasons by
geometry subtype, the counts of approximated curves and surfaces with the
largest fit tolerance, triangle counts and the read and conversion times, so a
caller can show what was and was not read.

## License

PolyForm Noncommercial 1.0.0, following inventor-kit; see [LICENSE](LICENSE).
Separate commercial licenses are available from UnRobotics Inc. Autodesk and
Inventor are trademarks of Autodesk, Inc.; this adapter ships no Autodesk
software.
