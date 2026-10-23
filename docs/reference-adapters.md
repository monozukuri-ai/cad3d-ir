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
