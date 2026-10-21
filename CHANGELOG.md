# Changelog

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
