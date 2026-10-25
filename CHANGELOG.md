# Changelog

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
