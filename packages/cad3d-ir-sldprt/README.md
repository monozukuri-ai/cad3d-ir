# cad3d-ir-sldprt

`cad3d-ir-sldprt` imports SOLIDWORKS parts (`.SLDPRT`) and assemblies
(`.SLDASM`) into `cad3d-ir` through
[sldkit](https://github.com/monozukuri-ai/sldkit). It does not read the
Parasolid B-Rep: it takes the triangle meshes that SOLIDWORKS saved for its
own display (`DisplayLists`), which sldkit returns as packed buffers, and
writes them as one glTF binary with the same conventions as the STEP adapter.
Document metadata (internal version, configuration names, custom properties,
units) and the saved preview image come from sldkit's bounded metadata parse.

Assemblies are placed from what the file stores. The display stream keeps one
record per component instance ahead of that instance's face tables, and the
component list carries each instance's `swTransform`; the adapter joins the
two and emits one glTF node per instance with its transform. On a four-valve
FESTO manifold this reproduced the STEP export of the same assembly within a
tenth of a millimetre (see `cad3d_ir_sldprt.assembly`).

The result is what SOLIDWORKS showed on screen when the file was saved: every
visible face at the saved image quality, face outlines as edges, saved face
colours as materials. Exact surfaces, dimensions and feature history are not
available from this path.

The distribution depends on `cad3d-ir` and `sldkit`; the core distribution
does not depend on this adapter or on sldkit.

## Usage

```python
from pathlib import Path

from cad3d_ir import DirectoryResourceWriter
from cad3d_ir.importers import ImportOptions
from cad3d_ir_sldprt import SldprtImporter

result = SldprtImporter().import_file(
    Path("bracket.SLDPRT"),
    options=ImportOptions(include_visualization_mesh=True, include_brep=False),
    resources=DirectoryResourceWriter("converted"),
)
preview = result.package.visualizations[0]  # geometry/<slug>-<key>.glb
print(preview.statistics["triangles"], preview.statistics["parts"])
for diagnostic in result.diagnostics:
    print(diagnostic.code, diagnostic.message)
```

```console
cad3d-ir-sldprt bracket.SLDPRT converted/                 # manifest + GLB + preview image
cad3d-ir-sldprt bracket.SLDPRT converted/ --no-mesh       # metadata and preview only
cad3d-ir-sldprt frame.SLDASM converted/                   # placed component instances
cad3d-ir-sldprt frame.SLDASM converted/ --no-assemblies   # reject assemblies instead
cad3d-ir-sldprt bracket.SLDPRT converted/ --diagnostics report.json
```

## What the package contains

- one `SourceDocument` (`format="sldprt"`, `version="SOLIDWORKS <year>"`
  estimated from the internal version) whose `properties` carry the envelope,
  document kind, internal version, configuration names, flattened custom
  properties, the document length unit and the display streams;
- one `part` product definition with one configuration named after the
  document's first configuration (or `Default`);
- one body whose `mesh` representation (`role="visualization"`,
  `fidelity="derived"`, `derivation.relation="tessellated"`) is the GLB, and a
  body appearance when the display data carries a saved colour;
- one package `Visualization` pointing at the same GLB, with `triangles`,
  `line_segments`, `vertices`, `nodes`, `meshes` and `parts` statistics;
- the saved preview image as a resource (`preview/<slug>-<key>.png` or
  `.bmp`, `properties.role="preview-image"`), referenced from
  `metadata.preview_resource_id`.

For an assembly the root product is an `assembly`, every component instance
that the display stream records becomes its own `part` product with a
configuration and (when it has geometry) a body, and the root configuration
owns one `Occurrence` per instance with the component transform in package
millimetres (`visible=False` for hidden components; suppressed components are
left out). Face tables that cannot be joined to a placed instance, such as
nested subassemblies, stay at their stored coordinates in a root body and are
reported.

The GLB has one shared vertex set in millimetres, one TRIANGLES primitive per
saved colour whose `extras.ranges` list `[pick_id, first_index, index_count]`
per face table, and one LINES primitive with the face outlines. The pick id
is the 1-based position of the face table in sldkit's `mesh.parts`. Node `0`
converts package units to glTF metres and Y-up exactly as the STEP adapter
does; SOLIDWORKS coordinates are passed through unchanged, so a part and the
STEP exported from it land in the same place.

## Options

`ImportOptions` (core): `include_visualization_mesh` writes the GLB (without
it the package carries metadata, the preview image and an empty body);
`mesh_include_edges` controls the outline primitive; `strict` turns a document
without any display data into `SldprtReadError`; `validate` runs the package
graph validation. The tessellation options do not apply: the mesh is the one
SOLIDWORKS saved.

`SldprtImporter(accept_assemblies=True, read_properties=True,
include_preview=True, limit_profile="service")`:

- `accept_assemblies=False` rejects `.SLDASM` documents with
  `UnsupportedDocumentKindError`. By default they are imported and placed.
- `read_properties=False` skips the metadata parse (a part still gets its
  kind and version from the display stream).
- `include_preview=False` leaves the preview image out.
- `limit_profile` is sldkit's limit profile (`service` is bounded for shared
  servers, `desktop` allows larger inputs).

## Diagnostics

| Code | Severity | Meaning |
| --- | --- | --- |
| `SLDPRT_DISPLAY_MESH_ONLY` | info | Geometry is the saved display tessellation; the B-Rep is not read. |
| `SLDPRT_APPEARANCE_NOT_SAVED` | info | No colour is saved with the display data; the default face colour is used. |
| `SLDPRT_COLORS_NOT_MAPPED` | info | Several saved colours are kept as GLB materials; the body appearance uses the most common one. |
| `SLDPRT_NORMALS_DERIVED` | info | A table without normals received area-weighted vertex normals. |
| `SLDPRT_CONFIGURATION_UNRESOLVED` | warning | Several display streams hold meshes; configuration ownership is not resolved and all are shown. |
| `SLDPRT_DISPLAY_STREAM_UNREADABLE` | warning | A display stream could not be decoded; the others are kept. |
| `SLDPRT_DISPLAY_ENCODING_UNSUPPORTED` | warning | The display stream uses an encoding sldkit does not decode (older `__Zip` streams). |
| `SLDPRT_VERTEX_BUDGET_EXCEEDED` | warning | The limit profile stopped packing vertices; the mesh is partial. |
| `SLDASM_PLACEMENT_FROM_COMPONENT_TRANSFORMS` | info | Component instances were placed with the assembly's stored `swTransform` values. |
| `SLDASM_PLACEMENT_UNRESOLVED` | warning | Some face tables could not be joined to a placed instance (nested subassemblies, unmatched records); they stay at stored coordinates. |
| `SLDASM_COMPONENTS_SKIPPED` | info | Hidden and suppressed component instances are left out of the visualization. |
| `SLDPRT_ASSEMBLY_PLACEMENT_UNVERIFIED` | warning | Only when nothing could be placed: the stored display tables are shown as they are. |
| `SLDPRT_PREVIEW_ONLY` | warning | No display mesh could be read; only the preview image and metadata are available. |
| `SLDPRT_PREVIEW_UNREADABLE` | warning | The saved preview image could not be extracted or decoded. |
| `SLDPRT_PROPERTIES_NOT_READ` | warning | The metadata parse failed; the mesh is still written. |
| `SLDPRT_NO_DISPLAY_DATA` | error | Neither display meshes nor a preview image exist; only metadata was read. |

Other `display.*` diagnostics from sldkit are passed through as
`SLDPRT_DISPLAY_<CODE>` with sldkit's severity and the original code in
`details.sldkit_code`.

## Current boundary

- Modern-container parts (SOLIDWORKS 2015 and later) and OLE2 parts whose
  display stream uses the `__ZLB` encoding yield meshes. OLE2 parts with the
  older `__Zip` encoding yield the preview image and metadata only.
- Drawings (`.SLDDRW`) are rejected. Assemblies are placed one level deep:
  nested subassemblies keep their stored coordinates and are reported, because
  their instance paths are not in the top-level component list.
- Configurations are not resolved: the display streams that SOLIDWORKS saved
  are returned, and a document with several mesh-bearing streams is flagged.
- Colours follow sldkit's saved precedence (body, feature, face); textures
  and appearances that SOLIDWORKS did not save with the display data are
  absent.
- The adapter is byte-faithful to sldkit: positions, normals and indices go
  into the GLB without conversion.

## Tests

The test suite builds minimal SolidWorks documents in memory (the same frame
and table grammar sldkit's own tests use) and ships no CAD file. Set
`CAD3D_IR_SLDPRT_SAMPLES` to a directory of local `.SLDPRT` files to run the
optional sample check.

## License

`cad3d-ir-sldprt` is source-available under the
[PolyForm Noncommercial License 1.0.0](LICENSES/PolyForm-Noncommercial-1.0.0.md),
following sldkit, with separate commercial licenses from **UnRobotics Inc.**
See [LICENSE](LICENSE). The `cad3d-ir` core and `cad3d-ir-step` in the same
repository remain MIT.

Required Notice: cad3d-ir-sldprt is licensed by UnRobotics Inc. (https://www.un-robotics.com/).
Required Notice: cad3d-ir-sldprt is offered under PolyForm Noncommercial 1.0.0; separate commercial licenses are available.
