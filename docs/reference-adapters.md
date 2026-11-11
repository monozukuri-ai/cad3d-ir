# Reference adapters and abstraction gates

## Dependency boundary

`cad3d-ir-step` is a separate distribution that depends on `cad3d-ir` and the
headless `cadquery-ocp-novtk` binding. The core distribution has no reverse
dependency on the adapter or Open CASCADE. Future native-format parsers should
likewise expose source-faithful typed models, with separate adapter packages
depending on both the parser and `cad3d-ir`.

## STEP AP242/XDE reference mapping

XDE is used because it distinguishes reusable shape definitions from located
assembly components and carries names and presentation data. The reference
mapping normalizes each XDE definition to a product with one resolved `Default`
configuration, each component path to an occurrence with a local transform,
and each leaf definition shape to a body backed by an external OCCT B-Rep
resource. STEP source units are normalized explicitly to package millimetres.

PMI/GD&T, presentation layers, and physical materials are detected but not yet
mapped. Their presence produces structured loss diagnostics instead of being
silently discarded. Configuration synthesis is also an explicit diagnostic.

## IGES through the same mapping

`IgesImporter` transfers IGES 5.x files with `IGESCAFControl_Reader` into the
same XDE document shape and reuses the STEP mapping. IGES files rarely carry
assembly structure, names or colours, so the resulting packages are usually a
flat list of parts; the units declared in the global section are reported in
`source_length_units` and normalized to package millimetres by OCCT.

## Display meshes

With `ImportOptions(include_visualization_mesh=True)` the adapter tessellates
every leaf definition once (`BRepMesh_IncrementalMesh`, linear deflection as a
ratio of the root bounding-box diagonal, parallel) and writes:

- one `mesh` representation per body (`role="visualization"`,
  `fidelity="derived"`, `derivation.relation="tessellated"`, with the B-Rep
  representation as `source_representation_id` when B-Rep is written); and
- one package `Visualization` whose GLB node tree mirrors the XDE occurrence
  tree. Occurrences of one definition reference the same glTF mesh, each node
  carries the IR occurrence id in `extras.id`, and every face and edge keeps a
  pick range so a viewer can resolve a picked triangle or segment back to the
  OCCT sub-shape index of that tessellation.

The GLB writer lives in the core (`cad3d_ir.mesh`) so that mesh-only importers
and future native-format adapters produce byte-compatible assets.

## SOLIDWORKS parts through sldkit display meshes

`cad3d-ir-sldprt` is the first native-format adapter. It depends on `sldkit`
and does not read the Parasolid B-Rep: the geometry is the tessellation that
SOLIDWORKS saved for its own display, returned by
`sldkit.decode_display_mesh_file` as packed `float32`/`uint32` buffers, which
the adapter hands to `GlbBuilder` unchanged. One shared vertex set carries the
positions and normals; one TRIANGLES primitive per saved colour lists
`[pick_id, first_index, index_count]` per face table in `extras.ranges`; one
LINES primitive holds the face outlines. Node `0` performs the same unit and
up-axis conversion as the STEP adapter, so a part and the STEP exported from
it coincide in a viewer.

The package is deliberately shallow: one source document with the internal
version, configuration names, custom properties and units from sldkit's
metadata parse; one part with one configuration; one body with a
`visualization` mesh representation and a body appearance when a colour was
saved; the saved preview image as a resource. Configurations are not
resolved (several mesh-bearing display streams are flagged), assemblies are
accepted only on request and flagged, drawings are rejected. Documents without
readable display data (older OLE2 `__Zip` streams) keep the metadata and the
preview image with a `SLDPRT_PREVIEW_ONLY` warning.

Its tests build SolidWorks documents in memory with the frame and table
grammar sldkit's tests use, so the repository ships no CAD file.

## eDrawings files through edrawings-kit display bundles

`packages/cad3d-ir-edrawings` maps eDrawings parts and assemblies (`.eprt`,
`.easm`: HOOPS Stream Files, alone or in a ZIP container) to the IR through
[edrawings-kit](https://github.com/monozukuri-ai/edrawings-kit). The kit's
`read_display` returns every body once as packed `float32`/`uint32` buffers in
the model's own units and every placement with a row-vector matrix, a component
name and path, a colour and its visibility, for one saved configuration and
display state. The adapter groups placements by name, path and matrix, groups
placements of the same bodies into one `part` product, and writes one GLB in
which each body is one vertex set, each distinct `(bodies, colours)` is one
mesh and each placement is one node with the converted column-vector matrix.
Positions and translations are scaled by the stream's `model_units`
(millimetres per unit; SOLIDWORKS writers omit it and store metres); axes are
kept. Placements are estimates, reported by `EDRAWINGS_PLACEMENT_ESTIMATED`.

Self-authored eDrawings 2026 fixtures (an OCCT box in millimetres, metres and
inches, and the `cad3d-ir-step` reference assembly) pin the unit rule and the
grouping; the rest of the suite builds HSF streams in memory. The package is
MIT, like the core.

## iCAD SX models through icadkit saved bodies

`packages/cad3d-ir-icad` maps one `.icd` file (iCAD SX keeps parts, the
assembly tree and the 2D drawing views in a single file) to the IR through
[icadkit](https://github.com/monozukuri-ai/icadkit):

- Every saved part record becomes a `ProductDefinition` with one
  `Configuration`; each child record is an `Occurrence` of its parent's
  configuration with the saved local transform, so the product tree is the
  iCAD part tree. External part references (other `.icd` files) are kept as
  products without bodies, listed as `SourceDependency` entries of the source
  document and reported by `ICD_EXTERNAL_PART_MISSING`.
- Geometry comes from `icadkit.read_part_shapes(frame="part")`: one OCCT
  compound per part from its saved final bodies (embedded Parasolid resources
  converted through parasolid-kit), else its V7L7 CSG result, else its native
  primitives. Parts whose solids are identical (same body payload hashes,
  source kinds, part-frame bounds and parity) share one glTF mesh; each part
  is one glTF node under its parent node. `include_brep` writes one OCCT BREP
  resource per distinct solid as an exact `brep` representation.
- `export_step` writes the same solids as a flat STEP assembly (one component
  per solid body, named after its part, shared referred products), using the
  STEP adapter's writer.
- Colours stay palette indices in iCAD and are not mapped
  (`ICD_APPEARANCE_NOT_MAPPED`); the 2D views are counted
  (`ICD_2D_VIEWS_PRESENT`) but belong to a 2D reader. Versions icadkit has
  not qualified fail closed (`ICD_VERSION_UNQUALIFIED`), as do legacy layouts
  without evaluable geometry (`ICD_LEGACY_VERSION_NO_GEOMETRY`).

The authored test models under `packages/cad3d-ir-icad/tests/data` were built
with icadkit's own fixture builders (a saved box, an external reference, a 2D
sheet); no iCAD-produced file is committed.

## Inventor parts through inventor-kit stored bodies

`packages/cad3d-ir-inventor` maps one Autodesk Inventor part (`.ipt`) to the
IR through [inventor-kit](https://github.com/monozukuri-ai/inventor-kit):

- The file becomes one `part` `ProductDefinition` with one `Configuration`
  named `Stored state`. A part has no occurrences. The source document's
  properties carry the segment major, the save version, the Inventor release
  derived from the major and the iProperties inventor-kit names.
- Geometry comes from `InventorDocument.convert_bodies(backend="occt")`: one
  OCCT solid per body of the stored B-Rep table, in millimetres, without
  CadQuery. Each converted body is one `Body` (`Body_<index>`), one glTF mesh
  and one node, built with `cad3d_ir_step.shapes.build_glb`. `include_brep`
  writes one OCCT BREP resource per body; it is `approximate` when stored
  B-spline fits stand in for procedural curves or surfaces
  (`INVENTOR_APPROXIMATED_GEOMETRY` gives their counts and the largest fit
  tolerance) and `exact` otherwise.
- Bodies that do not convert stay out of the package and are counted by
  outcome: geometry the converter does not build (`INVENTOR_BODY_UNSUPPORTED`,
  by geometry subtype), faces that do not close a volume
  (`INVENTOR_BODY_SHEET`) and failures (`INVENTOR_BODY_FAILED`).
- The stored table is mapped as it is. That it is the model state Inventor
  shows is not established (`INVENTOR_STATE_UNVERIFIED`,
  `properties.state_verified`); appearances are not read
  (`INVENTOR_APPEARANCE_NOT_READ`).
- A save generation outside those inventor-kit reads fails closed
  (`INVENTOR_SEGMENT_MAJOR_UNSUPPORTED`); one it frames like its verified
  neighbours is read with `INVENTOR_PROFILE_CANDIDATE`. Assemblies, drawings
  and presentations are reported (`INVENTOR_KIND_UNSUPPORTED`).
- The preview image the document stores is written as a `preview-image`
  resource whether or not a body converts, so a caller can fall back to it.
- `export_step` writes the converted solids as a flat STEP assembly, using the
  STEP adapter's writer.

The test parts under `packages/cad3d-ir-inventor/tests/data` were authored
with inventor-kit's part writer; no file produced by Inventor is committed.

## First abstraction fixture

The repository-owned AP242 fixture contains a root assembly, a base plate, and
a located pin-pair subassembly containing two occurrences of one reusable pin
definition. An independent typed synthetic native-CAD fixture describes the
same source semantics and is mapped without calling STEP or XDE code.

The gate compares the package coordinate convention, product kinds, resolved
configuration graph, definition reuse, occurrence names and local transforms,
body bounds, representation role/fidelity, and linear-sRGB body appearance. It
does not compare source provenance, generated identifiers, conversion method,
resource encoding, topology namespace, or binary bytes because these are
expected to differ by adapter.

This gate demonstrates that the common model can represent the same small
assembly through two independent mappings. It does not validate SOLIDWORKS
parsing, native configurations, suppression, mates, feature history, or
project/PDM reference resolution. Those remain subsequent fixture gates.
