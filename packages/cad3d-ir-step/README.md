# cad3d-ir-step

`cad3d-ir-step` imports STEP (AP203, AP214, AP242) and IGES files into
`cad3d-ir` through Open CASCADE XDE. It preserves product definitions,
assembly occurrences, local placements, names and surface colours, exports
each reusable leaf shape as an external OCCT B-Rep resource and, on request,
tessellates everything into glTF binary display meshes. Since 0.3.0 it also
writes STEP: a STEP or IGES document re-exported in another schema or unit,
or a faceted STEP built from the triangles of a visualization GLB. Since 0.4.0
that faceted STEP can also be written straight from the mesh topology, without
OCCT sewing, so large assemblies are no longer limited to 20,000 triangles.

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

AP242 tessellated geometry (`TRIANGULATED_FACE` and friends) is read as faces
that carry a triangulation and no surface: they appear in the display meshes
and are counted in the visualization statistics (`tessellated_faces` of
`faces`, diagnostic `STEP_TESSELLATED_FACES`); there is no B-rep to export for
them.

The command-line entry point picks the importer from the file content and
writes `manifest.json` plus geometry resources:

```console
cad3d-ir-step assembly.step converted/            # B-Rep resources only
cad3d-ir-step assembly.step converted/ --mesh     # + per-body GLB + package GLB
cad3d-ir-step robot.stp converted/ --mesh --no-brep --deflection-ratio 0.002
cad3d-ir-step part.igs converted/ --mesh
```

## Writing STEP

```python
from cad3d_ir_step import MeshTooLargeError, export_step, mesh_to_step

# STEP or IGES -> STEP in another schema / unit. Definition colours are copied
# onto the component labels first, so instanced parts keep their colour.
result = export_step("assembly.igs", "assembly.step", schema="AP242", unit="mm")
print(result.products, result.components, result.colors_copied, result.byte_length)

# Triangles of a cad3d-ir GLB -> one sewn solid per placed body (planar
# triangular faces; opens anywhere, not editable). Capped at 20,000 triangles
# because sewing time grows with the square of the count.
try:
    mesh = mesh_to_step(open("part.glb", "rb").read(), "part.step")
except MeshTooLargeError as exc:
    print(exc.triangles, exc.limit)
else:
    print(mesh.solids, mesh.open_shells, mesh.volume_mm3, mesh.diagnostics[0].code)
```

`export_step(verify=True)` reads the written file back and reports fewer
coloured shapes as `STEP_COLORS_CHANGED`; PMI that is not written is reported
as `STEP_PMI_NOT_WRITTEN`. `mesh_to_step(mode="tessellated")` writes AP242
`TRIANGULATED_FACE` entities instead of solids (about 80 bytes per triangle
instead of 2.4 KB), which not every CAD reads. Mesh results always carry
`STEP_MESH_SOLID_FACETED` or `STEP_MESH_TESSELLATED`; open shells are reported
as `STEP_MESH_FREE_EDGES`.

### Writing a mesh directly

```python
from cad3d_ir.mesh import read_glb
from cad3d_ir_step import estimate_mesh_step, write_mesh_step

scene = read_glb(open("assembly.glb", "rb").read())
estimate = estimate_mesh_step(scene)  # counted without writing
if estimate.byte_length < 200 * 2**20:
    result = write_mesh_step(scene, "assembly.step", schema="AP214")
    print(result.parts, result.placements, result.solids, result.open_shells)
```

`write_mesh_step` (AP214 or AP242) builds the topology from the mesh instead
of sewing OCCT faces, so its time grows linearly with the triangle count:

- each mesh is written once as a part and every node that places it becomes an
  assembly component; a placement that mirrors or scales its mesh is written
  as its own part (`STEP_MESH_PLACEMENTS_BAKED`);
- the vertex sets of a mesh are welded on identical positions, so faces that
  were tessellated separately join again; triangles that share an edge share
  one `EDGE_CURVE` and are oriented consistently;
- a part whose components all close is written as outward-oriented
  `MANIFOLD_SOLID_BREP`s; otherwise as a `SHELL_BASED_SURFACE_MODEL`
  (`STEP_MESH_FREE_EDGES`);
- every triangle is an `ADVANCED_FACE` on a `PLANE` with `LINE` edges (about
  600–800 bytes per written triangle; `edge_curve="polyline"` is about a fifth
  smaller but slower to read in OCCT);
- each solid or surface model takes its most common colour and triangles of
  another colour are styled individually; names come from the GLB meshes and
  nodes.

`estimate_mesh_step` returns the triangles that would be written (each mesh
once), the triangles once every placement is expanded, and a rough size
(`DIRECT_BYTES_PER_TRIANGLE` per triangle).

`write_mesh_step(..., schema="AP242", representation="tessellated")` writes the
same parts and placements as AP242 tessellated geometry instead of B-rep: one
`COORDINATES_LIST` of welded points per part, a `TRIANGULATED_FACE` per vertex
set of the source mesh (one CAD face, for writers that tessellate per face)
with the mesh's own vertex normals (`normals=False` leaves them out), grouped
into a `TESSELLATED_SOLID` where the triangles close and a `TESSELLATED_SHELL`
elsewhere. It is about a tenth of the size of the B-rep form (60–70 bytes per
triangle with normals, 35–40 without) and loads in seconds where the B-rep
takes minutes, but CAD shows it as a mesh (a graphics body): there are no
faces to edit or measure (`STEP_MESH_TESSELLATED`). Open CASCADE reads it with
`read.step.tessellated` set to 1 (FreeCAD does so), eDrawings and other HOOPS
Exchange readers directly. `estimate_mesh_step(scene,
representation="tessellated")` sizes it from the `TESSELLATED_BYTES_PER_*`
constants.

```console
cad3d-ir-step-export assembly.step assembly-ap203.step --schema AP203
cad3d-ir-step-export part.igs part.step --unit inch --verify
cad3d-ir-step-export preview.glb part.step --max-triangles 20000
cad3d-ir-step-export preview.glb part.step --mode tessellated
cad3d-ir-step-export preview.glb assembly.step --mode direct
cad3d-ir-step-export preview.glb mesh.step --mode direct --schema AP242 --representation tessellated
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
