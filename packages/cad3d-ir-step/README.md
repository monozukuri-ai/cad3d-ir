# cad3d-ir-step

`cad3d-ir-step` imports STEP (AP203, AP214, AP242) and IGES files into
`cad3d-ir` through Open CASCADE XDE. It preserves product definitions,
assembly occurrences, local placements, names and surface colours, exports
each reusable leaf shape as an external OCCT B-Rep resource and, on request,
tessellates everything into glTF binary display meshes.

The runtime uses the official `cadquery-ocp-novtk` binding because conversion
does not require VTK or a visualization stack.

The distribution intentionally depends on `cad3d-ir`; the core distribution
does not depend on this adapter or on Open CASCADE.

## Usage

```python
from pathlib import Path

from cad3d_ir import DirectoryResourceWriter
from cad3d_ir.importers import ImportOptions
from cad3d_ir_step import IgesImporter, StepImporter

output = Path("converted")
result = StepImporter().import_file(
    Path("assembly.step"),
    options=ImportOptions(include_visualization_mesh=True),
    resources=DirectoryResourceWriter(output),
)
preview = result.package.visualizations[0]  # preview/<package>.glb
IgesImporter().import_file(Path("part.igs"), options=ImportOptions(), resources=...)
```

The command-line entry point picks the importer from the file content and
writes `manifest.json` plus geometry resources:

```console
cad3d-ir-step assembly.step converted/            # B-Rep resources only
cad3d-ir-step assembly.step converted/ --mesh     # + per-body GLB + package GLB
cad3d-ir-step robot.stp converted/ --mesh --no-brep --deflection-ratio 0.002
cad3d-ir-step part.igs converted/ --mesh
```

## Options

`ImportOptions` (core):

- `include_visualization_mesh` – write one `mesh` representation per body and
  one package-level `Visualization` (GLB). Off by default.
- `include_brep` – write exact OCCT B-Rep ASCII resources. On by default.
- `mesh_linear_deflection_ratio` – linear deflection as a ratio of the root
  bounding-box diagonal (default `0.001`); `mesh_angular_deflection` in
  radians (default `0.3`); `mesh_include_edges` for edge polylines.

`StepImporter(require_ap242=False, body_mesh_resources=True)`:

- `require_ap242=True` restores the 0.1 behaviour of rejecting other schemas
  before any geometry is transferred. By default AP203/AP214 files are read
  and an informational `STEP_SCHEMA_NOT_AP242` diagnostic is emitted.
- `body_mesh_resources=False` keeps only the package GLB when meshes are
  requested (what a viewer needs; halves the written bytes).

## Current boundary

- STEP Part 21 files with any AP schema and IGES 5.x files are accepted; the
  mapping is the same XDE product graph for both.
- XDE product structure, names, local placements, visibility and definition
  surface colours are mapped. IGES carries little structure, so its packages
  are usually one part per shape.
- Leaf definition shapes are stored as OCCT B-Rep ASCII v1 resources
  (`include_brep`), as glTF binary meshes (`include_visualization_mesh`), or
  both.
- One resolved `Default` configuration is synthesized for every product.
- PMI/GD&T, layers, physical materials and external STEP references are not
  mapped; they produce `*_NOT_MAPPED` diagnostics instead of being dropped
  silently. Faces that OCCT cannot triangulate are counted in
  `*_MESH_INCOMPLETE`.
- Representation bounds come from `BRepBndLib::Add` (surface control points),
  which is conservative; the visualization bounds are exact mesh bounds.

Measured on a 47 MB industrial robot STEP (12,817 faces): read 28 s, meshing
4 s, extraction and GLB 5 s, peak RSS about 730 MB, 27.6 MB GLB with 700k
triangles.

## Fixtures

`tests/fixtures/reference-assembly.step` (AP242), `reference-assembly-ap214.step`
and `reference-assembly.iges` are generated from primitive boxes and cylinders
by `scripts/generate_reference_fixture.py --schema {ap242,ap214,iges}` with
normalized dates and author fields. The abstraction test compares the AP242
assembly with an independent typed synthetic native-CAD source fixture.
