# Changelog

## Unreleased

### cad3d-ir-sldprt 0.4.0

- Places the assemblies that SOLIDWORKS 2023 and later save with
  `FaceTessellations` sections (sldkit 0.3.3 returns their directory as
  `mesh.instances` and tags each table with its owner). Every occurrence with
  tables becomes an occurrence of the package: a top-level one takes its
  component's `swTransform`, a repeated one shares the owner's tables, and a
  nested one composes the sub-assembly transform with the child `swReference`
  transforms read from `swXmlContents/COMPINSTANCETREE`, root first. Before
  this the adapter saw no tables in these files and fell back to the preview
  image.
- Instances that display the same face tables share one glTF mesh under
  separate nodes, so a pattern of twenty bolts carries one copy of the bolt.
  The mesh `extras.id` names the first instance's body.
- `SLDASM_PLACEMENT_FROM_COMPONENT_TRANSFORMS` and
  `SLDASM_PLACEMENT_UNRESOLVED` report `source` (`display_lists` or
  `face_tessellations`); the former also counts the `nested` occurrences it
  placed. The package metadata and statistics add `placement_source` and
  `nested_placed_instances`. Nested occurrences are named
  `<sub>-<m>/<name>-<n>`.
- The directory keeps the occurrence names from the time the tessellation
  was saved, so after the components were renamed no name matches the
  component list. When the directory lists exactly the component tree's part
  occurrences in order (same count, instance numbers and nesting depth), the
  unmatched occurrences are placed by that position and
  `SLDASM_PLACEMENT_BY_TREE_ORDER` (warning, `approximated`) says so;
  otherwise they stay unplaced as before.
- An instance without a placement no longer becomes an occurrence with an
  identity transform drawn beside the root body's copy of the same tables;
  its tables stay in the root body only (`SLDASM_PLACEMENT_UNRESOLVED`).
- Requires `sldkit>=0.3.3`.

### cad3d-ir-icad 0.3.1

- Accepts `cad3d-ir-step` 0.4, which adds the direct mesh writer and leaves
  the `export` and `tessellate` modules this adapter uses unchanged. The
  0.3.0 pin (`<0.4`) made the adapter uninstallable next to cad3d-ir-step
  0.4.0.
- The README and the tests follow icadkit 0.3.14, which qualified V8L4: the
  example of a version that fails closed with `ICD_VERSION_UNQUALIFIED` is now
  V8L5. Which versions are qualified is icadkit's
  (`icadkit.qualified_versions()`), not the adapter's.

### cad3d-ir-parasolid 0.3.1

- Accepts `cad3d-ir-step` 0.4 for the same reason as cad3d-ir-icad 0.3.1.

### cad3d-ir-parasolid 0.3.0 (new)

- New workspace member `packages/cad3d-ir-parasolid`: an importer for
  Parasolid transmit files (`.x_t`, `.x_b`, `.xmt_txt`, `.xmt_bin`) through
  `parasolid-kit[occt]>=0.3.12`. The file's own B-Rep is converted to exact
  OCCT bodies (metres to millimetres; the faces whose construction fails are
  dropped and reported, the body is kept as an open shell, and a shape that
  OCCT's BRepCheck rejects is shown as built with a warning), the assembly
  instances the file stores become occurrences with their transforms, the
  `SDL/TYSA_COLOUR` and `SDL/TYSA_NAME` attributes become body and face
  colours and body names, and one GLB in millimetres shares a mesh between
  the placements of a body. `export_step` writes the converted bodies as a
  named, coloured STEP assembly. Files whose schema key no built-in profile
  covers fail closed with `PARASOLID_SCHEMA_UNSUPPORTED`, naming the stream's
  own key (read with parasolid-kit's header inspection, since the common
  header's `SCH=` line names the writer's native schema instead); the
  writer's application and Parasolid version are reported from the common
  header. Reported through `PARASOLID_*` diagnostics and statistics; MIT,
  like the core.

### cad3d-ir-step 0.4.2

- `StepImporter` sets `read.step.tessellated` explicitly, so AP242 tessellated
  geometry (`TRIANGULATED_FACE`) is read whatever the OCCT default is. Faces
  that carry only tessellation (no surface) are counted in the package
  visualization's statistics (`faces`, `tessellated_faces`) and reported with
  `STEP_TESSELLATED_FACES`.
- `export_step` writes tessellated-only faces only when the schema is AP242
  (`STEP_TESSELLATED_WRITTEN`). AP203 and AP214 have no tessellated entities;
  such faces are now left out with `STEP_TESSELLATED_NOT_WRITTEN` instead of
  being written as AP242 entities inside an AP203 / AP214 file. The result
  reports `faces` and `tessellated_faces`.

### cad3d-ir-step 0.4.1

- `write_mesh_step(..., schema="AP242", representation="tessellated")` writes
  the parts and placements of the direct writer as AP242 tessellated geometry:
  one `COORDINATES_LIST` of welded points per part, a `TRIANGULATED_FACE` per
  vertex set of the source mesh carrying its vertex normals (`normals=False`
  leaves them out), grouped into a `TESSELLATED_SOLID` where the triangles
  close and a `TESSELLATED_SHELL` elsewhere, inside a
  `TESSELLATED_SHAPE_REPRESENTATION`. The result reports
  `representation` and `tessellated_faces`, and its first diagnostic is
  `STEP_MESH_TESSELLATED` instead of `STEP_MESH_SOLID_FACETED`. About a tenth
  of the size of the B-rep form; Open CASCADE reads it with
  `read.step.tessellated` on, HOOPS Exchange readers (eDrawings) directly.
- `estimate_mesh_step(scene, representation=..., normals=...)` sizes that
  form from `TESSELLATED_BYTES_PER_TRIANGLE`, `TESSELLATED_BYTES_PER_VERTEX`
  and `TESSELLATED_BYTES_PER_NORMAL`, and now reports the vertex count.
- `cad3d-ir-step-export --mode direct` takes `--representation tessellated`
  and `--no-normals`.

### cad3d-ir-step 0.4.0

- `write_mesh_step` writes the triangles of a cad3d-ir GLB scene as STEP
  (AP214, AP242) without OCCT sewing, so its time grows linearly with the
  triangle count and large meshes are no longer bound by the 20,000-triangle
  cap of `mesh_to_step`. Each mesh is written once as a part and every node
  that places it becomes an assembly component; a placement that mirrors or
  scales its mesh is written as its own part (`STEP_MESH_PLACEMENTS_BAKED`).
  The vertex sets of a mesh are welded on identical positions, triangles that
  share an edge share one `EDGE_CURVE` and are oriented consistently, and a
  part whose components all close becomes outward-oriented
  `MANIFOLD_SOLID_BREP`s (otherwise a `SHELL_BASED_SURFACE_MODEL`, reported as
  `STEP_MESH_FREE_EDGES`). Every triangle is an `ADVANCED_FACE` on a `PLANE`
  with `LINE` edges (`edge_curve="polyline"` for two-point `POLYLINE`s).
  Colours (the most common per body, the others per face) and names follow
  the GLB.
- `estimate_mesh_step` counts the triangles that would be written, the
  triangles once every placement is expanded and a rough size
  (`DIRECT_BYTES_PER_TRIANGLE`).
- `cad3d-ir-step-export --mode direct` writes GLB sources this way.
  `--max-triangles` now defaults to the 20,000 cap only for the sewn and
  tessellated modes.

### cad3d-ir-step 0.3.1

- Free edges mesh again. Wireframe IGES files (drawing curves, construction
  lines) can have edges that belong to no face. `TopExp::MapShapesAndAncestors` registers
  them with an empty face list and the edge extraction called `First()` on
  it, so the whole import failed with `Standard_NoSuchObject:
  NCollection_List::First`. Such edges now use the polygon `BRepMesh` stored
  for them, or are discretised from the curve with the same deflections.

### cad3d-ir-edrawings 0.3.0 (new)

- New workspace member `packages/cad3d-ir-edrawings`: an importer for
  eDrawings parts and assemblies (`.eprt`, `.easm`; `.edrw` drawings on
  request, 3D bodies only) through `edrawings-kit>=0.2`. The kit's display
  bundle (bodies as packed buffers, estimated placements, colours, the shown
  saved configuration, the preview image) becomes one package: placements that
  share a component name, path and matrix form occurrences, placements of the
  same bodies share a `part` product, and one GLB in millimetres shares vertex
  data between them. Coordinates keep the model's axes and are scaled by the
  stream's `model_units` (SOLIDWORKS writers store metres). Reported through
  `EDRAWINGS_*` diagnostics and statistics; MIT, like the core.

### cad3d-ir-icad 0.3.0 (new)

- New workspace member `packages/cad3d-ir-icad`: an importer for iCAD SX
  models (`.icd`) through `icadkit[preview]>=0.3.8`. Saved final bodies
  (and CSG results or native primitives when a part has none) become exact
  OCCT solids placed by the saved part frames; the package has one product per
  part record, the part tree as occurrences, one GLB visualization with meshes
  shared between identical parts, optional BREP resources, and `export_step`
  for a STEP assembly of the same solids. External references, unqualified
  versions, unconverted bodies, palette colours and the 2D views of the file
  are reported through `ICD_*` diagnostics and statistics. PolyForm
  Noncommercial, following icadkit.

## 0.3.0 — 2026-10-25

### cad3d-ir (core, IR schema 0.2.0)

- Added `cad3d_ir.mesh.read_glb` and `MeshScene`: the GLBs written by
  `GlbBuilder` are read back into nodes with world matrices, meshes and
  primitives in package coordinates. The `extras.cad3d_ir` root is recognised
  and skipped; a GLB from another writer is converted from glTF metres / Y-up
  to millimetres / Z-up. `placed_vertex_sets()` hands out positions and
  normals transformed by each placement, and the `placed_*` counts count a
  mesh once per node that places it.
- Added `cad3d_ir.exporters`: `write_stl` (binary or ASCII, face normals,
  zero-area triangles dropped and counted) and `write_obj` (one `o` per placed
  node, a shared vertex set written once, `v` / `vn` / `f`, optional `l` edge
  records, no MTL). Both take a scene or the GLB bytes and return `MeshExport`
  with the bytes, counts and bounds. `estimate_stl_bytes` and
  `estimate_obj_bytes` size an output before it is written.
- Added the `cad3d-ir export` CLI command (`--stl`, `--ascii-stl`, `--obj`,
  `--no-normals`, `--no-groups`, `--lines`).

### cad3d-ir-sldprt 0.3.0

- Requires `cad3d-ir>=0.3,<0.4`. Includes the 0.2.2 changes below, which were
  never published on their own.

### cad3d-ir-step 0.3.0

- Added `export_step`: a STEP or IGES file is read into XCAF again and written
  with `STEPCAFControl_Writer` as AP203, AP214 or AP242 in a chosen unit.
  Definition colours are copied onto component labels before writing, because
  the writer otherwise drops the surface colour of instanced shapes;
  `verify=True` reads the result back and reports `STEP_COLORS_CHANGED`.
  PMI that is not written is reported as `STEP_PMI_NOT_WRITTEN`.
- Added `mesh_to_step`: the triangles of a cad3d-ir GLB (or `MeshBody` items)
  become one sewn solid per placed body with the node names and colours
  (`MakeShapeOnMesh` → `Sewing` → `MakeSolid`, oriented and checked). Open
  shells are kept as shells and reported (`STEP_MESH_FREE_EDGES`); the result
  always carries `STEP_MESH_SOLID_FACETED`. A triangle cap (default 20,000)
  raises `MeshTooLargeError` before geometry is built. `mode="tessellated"`
  writes AP242 triangulated faces instead.
- Added the `cad3d-ir-step-export` command. OCCT's writer progress output is
  silenced while writing. Requires `cad3d-ir>=0.3,<0.4`.

## cad3d-ir-sldprt 0.2.2 — 2026-10-23

- Assemblies (`.SLDASM`) are imported by default and placed: each face table
  is joined to its component instance through the instance records of the
  display stream and moved with the component's `swTransform` (row-major 4x4
  in metres for row vectors), giving one glTF node and one IR occurrence per
  instance. Hidden instances become invisible occurrences, suppressed ones are
  left out, nested subassemblies stay at stored coordinates and are reported
  (`SLDASM_PLACEMENT_UNRESOLVED`). Verified against the STEP export of a
  four-valve manifold (median nearest-neighbour distance 0.16 mm).
- `accept_assemblies` now defaults to `True`; `False` rejects assemblies.
- The CLI gained `--no-assemblies`.

## 0.2.1 — 2026-10-23

### cad3d-ir (core, IR schema 0.2.0)

- `GlbBuilder.add_triangles` and `add_lines` accept packed little-endian
  buffers (`bytes`, `bytearray`, `memoryview`, `array.array`): `float32`
  triples for positions and normals, `uint32` for indices. Buffers are written
  without being expanded into Python objects; sequences still work and produce
  identical GLB bytes.
- `GlbBuilder.add_vertices` registers a vertex set that several primitives
  share through `vertices=`, so face groups and edge outlines of one body no
  longer duplicate positions. Shared vertices count once towards
  `vertex_count` and `bounds`.
- Index validation now reports negative indices as missing vertices for
  sequences and buffers alike.

### cad3d-ir-sldprt (new, 0.2.1)

- SOLIDWORKS part importer through sldkit 0.3: the saved display meshes become
  one GLB (shared vertex set, one TRIANGLES primitive per saved colour with
  per-face-table pick ranges, face outlines as LINES) with the STEP adapter's
  node and unit conventions. Document metadata (internal version, estimated
  release year, configuration names, custom properties, units) and the saved
  preview image (PNG, or DIB with a BMP header) are carried as source
  properties and a resource.
- Diagnostics are prefixed `SLDPRT_*`; the B-Rep is never read
  (`SLDPRT_DISPLAY_MESH_ONLY`), documents without display data keep metadata
  and preview (`SLDPRT_PREVIEW_ONLY` / `SLDPRT_NO_DISPLAY_DATA`), assemblies are
  opt-in and flagged, drawings are rejected.
- Licensed under PolyForm Noncommercial 1.0.0 by UnRobotics Inc., following
  sldkit; the core and `cad3d-ir-step` stay MIT.

## 0.2.0 — 2026-10-21

### cad3d-ir (core, IR schema 0.2.0)

- Added `Visualization` and `CadPackage.visualizations`: package- or
  configuration-scoped, display-only derived assets (currently glTF binary)
  with resource reference, bounds, derivation and statistics. Graph validation
  checks their resource and configuration references.
- Added `cad3d_ir.mesh`: a dependency-free GLB 2.0 writer (`GlbBuilder`,
  shared meshes, node tree, TRIANGLES / LINES / LINE_STRIP primitives, root
  unit and up-axis conversion), `validate_glb_bytes`, and triangle helpers.
- Added the built-in `MeshImporter` for STL (ASCII and binary) and Wavefront
  OBJ, plus the `cad3d-ir import` CLI command.
- `ImportOptions` gained `include_brep`, `mesh_linear_deflection_ratio`,
  `mesh_angular_deflection` and `mesh_include_edges`.

### cad3d-ir-step

- `include_visualization_mesh` is implemented: one `mesh` representation per
  body (optional, `body_mesh_resources`) and one package-level GLB whose node
  tree mirrors the XDE occurrences and shares meshes between occurrences of
  one definition. Face and edge pick ranges are kept in primitive `extras`.
- STEP AP203 and AP214 files are imported by default; `require_ap242=True`
  restores the strict schema gate.
- Added `IgesImporter` (IGES 5.x through `IGESCAFControl_Reader`) and header
  inspection of the IGES global section (units).
- Representation bounds use `BRepBndLib::Add` instead of the optimal box,
  which took two minutes on a 47 MB assembly.
- The CLI accepts IGES and gained `--mesh`, `--no-brep`, `--deflection-ratio`
  and `--require-ap242` (replacing `--allow-non-ap242`).
- Diagnostic codes are prefixed per format (`STEP_*`, `IGES_*`).

## 0.1.0

- Initial typed IR, validation, manifest IO, resource writer, and the STEP
  AP242/XDE reference adapter.
