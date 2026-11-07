# cad3d-ir-edrawings

`cad3d-ir-edrawings` imports eDrawings parts (`.eprt`) and assemblies
(`.easm`) into `cad3d-ir` through
[edrawings-kit](https://github.com/monozukuri-ai/edrawings-kit). An eDrawings
file is a HOOPS Stream File (HSF), on its own or inside a ZIP container
(sometimes behind a short preamble). edrawings-kit reads it with bounded
resources and returns a *display bundle*: every body once as packed buffers,
every placement of a body with its estimated matrix, name and colour, the saved
configuration that is shown, and the preview image. The adapter writes that
bundle as one glTF binary with the same conventions as the STEP adapter and
describes it as products, occurrences and bodies.

The geometry is the tessellation eDrawings saved; there is no B-Rep in these
files. Placements are estimates: they follow the stored segment tree, modelling
matrices, includes and repeat instances, and eDrawings' own arrangement is not
guaranteed to match (`EDRAWINGS_PLACEMENT_ESTIMATED`). On the project's own
fixtures (a box written in millimetres, metres and inches, and the reference
assembly of `cad3d-ir-step`) the bounds and placements match the STEP sources
to within a micrometre.

The distribution depends on `cad3d-ir` and `edrawings-kit` (MIT / Apache-2.0,
an independent implementation of the public HSF specification); the core
distribution does not depend on this adapter.

## Usage

```python
from pathlib import Path

from cad3d_ir import DirectoryResourceWriter
from cad3d_ir.importers import ImportOptions
from cad3d_ir_edrawings import EdrawingsImporter

result = EdrawingsImporter().import_file(
    Path("frame.easm"),
    options=ImportOptions(include_visualization_mesh=True, include_brep=False),
    resources=DirectoryResourceWriter("converted"),
)
visualization = result.package.visualizations[0]  # geometry/<slug>-<key>.glb
print(visualization.statistics["triangles"], visualization.statistics["placements"])
for diagnostic in result.diagnostics:
    print(diagnostic.code, diagnostic.message)
```

```console
cad3d-ir-edrawings frame.easm converted/                        # manifest + GLB + preview image
cad3d-ir-edrawings part.eprt converted/ --no-mesh               # metadata and preview only
cad3d-ir-edrawings frame.easm converted/ --configuration Beta   # another saved configuration
cad3d-ir-edrawings frame.easm converted/ --include-hidden       # keep hidden components as invisible occurrences
cad3d-ir-edrawings sheet.edrw converted/ --accept-drawings      # 3D bodies of a drawing, sheets ignored
cad3d-ir-edrawings frame.easm converted/ --diagnostics report.json
```

## Units and axes

Coordinates keep the model's own axes. The `model_units` option of the stream
(millimetres per model unit: `1` mm, `1000` m, `25.4` inch) scales positions
and translations to package millimetres; SOLIDWORKS-written files omit the
option and store metres, which is the default. Matrices are converted from the
kit's row-vector convention to cad3d-ir's column-vector matrices. Node `0` of
the GLB converts millimetres and Z-up to glTF conventions exactly as the STEP
adapter does, so an eDrawings file and the STEP exported from the same model
land in the same place.

## What the package contains

- one `SourceDocument` (`format="edrawings"`, `version` = the writer string such
  as `SWeModels-30.4.0.0045-2022207`) whose `properties` carry the container
  kind, HSF version, writer, writer year hint, mount path, model name,
  `model_units`, the container entries and the saved configurations;
- a root product (`assembly` for `.easm`, `part` for `.eprt`) with one
  configuration named after the shown saved configuration;
- one `part` product per set of bodies that is placed together: the kit's
  instances are grouped by component name, path and matrix into placements, and
  placements of the same bodies share a product (`SCR083-9` and `SCR083-10`
  become two occurrences of `SCR083`). Each product has one configuration with
  its bodies; a body carries the most common saved colour as its appearance;
- one `Occurrence` per placement under the root configuration, with the matrix
  in package millimetres and `visible=False` for components eDrawings hides
  (only with `include_hidden=True`; by default they are left out);
- one package `Visualization` pointing at the GLB with `triangles` (placed),
  `mesh_triangles` (unique), `line_segments`, `vertices`, `nodes`, `meshes`,
  `placements` and `bodies` statistics;
- the saved preview image as a resource (`preview/<slug>-<key>.jpg`,
  `properties.role="preview-image"`), referenced from
  `metadata.preview_resource_id`.

The GLB writes each kit body once as a vertex set in millimetres (stored
normals kept). A glTF mesh exists per distinct `(bodies, colours)` of a
placement, with one TRIANGLES primitive per body (`extras.ranges` =
`[[pick_id, 0, index_count]]`, the pick id being the 1-based kit body index)
and one LINES primitive per body for the stored outline edges. One node per
placement carries the occurrence id in `extras.id` and the converted matrix;
placements of the same product with the same colours share the mesh.

## Options

`ImportOptions` (core): `include_visualization_mesh` writes the GLB (without it
the package carries products, occurrences, bodies and the preview);
`mesh_include_edges` controls the outline primitives; `strict` turns a document
without 3D bodies and without a preview image into `EdrawingsReadError`;
`validate` runs the package graph validation. The tessellation options do not
apply: the mesh is the one eDrawings saved.

`EdrawingsImporter(accept_drawings=False, include_preview=True,
configuration=None, display_state=None, include_hidden=False,
limit_profile="service", limits=None)`:

- `accept_drawings=True` reads the 3D bodies stored with `.edrw` drawings; the
  2D sheets are not read (`EDRAWINGS_DRAWING_SHEETS_IGNORED`). By default
  drawings raise `UnsupportedDocumentKindError`.
- `configuration` shows a saved configuration (its `config name` or `configN`
  segment) instead of the file's active one; `display_state` applies a saved
  display state (`dispN` or its name) of it. Unknown names raise
  `UnknownSelectionError`.
- `include_hidden=True` keeps components eDrawings hides as invisible
  occurrences; they stay out of the GLB.
- `limit_profile` is the edrawings-kit resource profile: `service` keeps the
  kit's traversal, vertex and instance caps for shared servers but lifts the
  container and XML sizes large assemblies reach (a 2 MB `materials.xml`
  holds more than the kit's default 20,000 elements); `desktop` allows larger
  inputs. `limits` passes an explicit `edrawings_kit.Limits`. Exceeding a limit
  raises `EdrawingsLimitError`.

## Diagnostics

| Code | Severity | Meaning |
| --- | --- | --- |
| `EDRAWINGS_DISPLAY_MESH_ONLY` | info | Geometry is the saved tessellation; no exact surfaces exist in the file. |
| `EDRAWINGS_PLACEMENT_ESTIMATED` | info | Placements follow the stored scene graph; `details.withheld_by` lists the kit codes that keep the result partial. |
| `EDRAWINGS_CONFIGURATION_SELECTED` | info | Which saved configuration and display state are shown and how they were selected. |
| `EDRAWINGS_HIDDEN_COMPONENTS_SKIPPED` | info | Components eDrawings hides are left out of the GLB (and of the occurrences unless `include_hidden`). |
| `EDRAWINGS_UNPLACED_BODIES` | warning | Bodies without a placement in the shown configuration are not drawn. |
| `EDRAWINGS_LINE_ONLY_BODIES_SKIPPED` | info | Placements that carry only line geometry are left out. |
| `EDRAWINGS_APPEARANCE_NOT_SAVED` | info | No colour is stored for the shown bodies; the default face colour is used. |
| `EDRAWINGS_COLOR_FROM_MATERIALS` | info | Colours come from `materials.xml` or component options rather than the HSF stream. |
| `EDRAWINGS_RENDER_SETTINGS_IGNORED` | info | Stored render settings (lighting, display modes, line styles) are not evaluated. |
| `EDRAWINGS_MATERIALS_UNREADABLE` | warning | `materials.xml` could not be read; colours fall back to the stream. |
| `EDRAWINGS_STOPPED_AT` | warning | The stream could not be read to its end; bodies read before the stop are kept (`details.record_id`). |
| `EDRAWINGS_READER_DIAGNOSTICS` | info | Counts of the further edrawings-kit diagnostics (`details.codes`). |
| `EDRAWINGS_VERSION_UNSUPPORTED` | error | edrawings-kit could not read the display bundle of this HSF version; metadata and preview only. |
| `EDRAWINGS_PREVIEW_ONLY` | warning | No 3D body could be read; only the preview image and metadata are available. |
| `EDRAWINGS_PREVIEW_UNREADABLE` | warning | The saved preview image could not be extracted or decoded. |
| `EDRAWINGS_NO_GEOMETRY` | error | Neither 3D bodies nor a preview image exist; only metadata was read. |
| `EDRAWINGS_DRAWING_SHEETS_IGNORED` | warning | The file is a drawing; its 2D sheets are not read. |

## Current boundary

- Parts and assemblies written by eDrawings (SOLIDWORKS `SWeModels-*` writers,
  HOOPS Exchange based writers such as eDrawings 2026, and older Pro/ENGINEER
  publishers down to HSF 8.05) yield bodies and placements.
  Drawings yield only the 3D bodies they may carry; their sheets are a later
  stage.
- Placements are estimated from the scene graph (see above). One saved
  configuration and one display state are shown per import.
- Colours follow the kit's precedence (inherited HSF colour, `materials.xml`
  entity, component `defaultcolor`); textures are not read.
- The adapter is byte-faithful to the kit apart from the unit scale: normals and
  indices go into the GLB without conversion.

## Tests

The test suite builds minimal HSF streams and containers in memory with the
grammar edrawings-kit's own tests use, and ships five self-authored eDrawings
2026 files (see `tests/fixtures/README.md`). Set `CAD3D_IR_EDRAWINGS_SAMPLES` to
a directory of local `.eprt`/`.easm` files to run the optional sample check.

## License

MIT, like `cad3d-ir` and `cad3d-ir-step`.
