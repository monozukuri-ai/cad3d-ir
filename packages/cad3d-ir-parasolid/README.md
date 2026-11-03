# cad3d-ir-parasolid

`cad3d-ir-parasolid` imports Parasolid transmit files (`.x_t`, `.x_b`,
`.xmt_txt`, `.xmt_bin`) into `cad3d-ir` through
[parasolid-kit](https://github.com/monozukuri-ai/parasolid-kit). parasolid-kit
parses the file with its built-in schema profiles (no Parasolid installation
or schema catalog) and converts the B-Rep into exact OCCT bodies; the adapter
scales them from the file's metres to millimetres, drops the faces whose
construction fails (the body is kept as an open shell and the loss is
reported), places the bodies by the assembly instances the file stores,
colours and names them from the `SDL/TYSA_COLOUR` and `SDL/TYSA_NAME`
attributes and writes one glTF binary with the same conventions as the STEP
adapter. Because the bodies are exact B-Rep, the same package can be written
as a STEP assembly (`export_step`), with the names and colours.

What the adapter reports but does not map: attributes other than the system
colour and name, negative instances (placed like positive ones), general
affine or perspective instance transforms. A file whose schema key no built-in
profile covers (a Parasolid version parasolid-kit has not reviewed) fails
closed with `PARASOLID_SCHEMA_UNSUPPORTED`, naming the writing application
and Parasolid version from the file's common header.

The distribution depends on `cad3d-ir`, `cad3d-ir-step` (tessellation and the
STEP writer) and `parasolid-kit[occt]`; the core distribution does not depend
on this adapter.

## Usage

```python
from pathlib import Path

from cad3d_ir import DirectoryResourceWriter
from cad3d_ir.importers import ImportOptions
from cad3d_ir_parasolid import ParasolidImporter, export_step

result = ParasolidImporter().import_file(
    Path("part.x_t"),
    options=ImportOptions(include_visualization_mesh=True, include_brep=False),
    resources=DirectoryResourceWriter("converted"),
)
print(result.statistics["bodies_converted"], result.statistics["triangles"])
for item in result.diagnostics:
    print(item.code, item.message)

export_step(Path("part.x_t"), Path("part.step"), schema="AP214")
```

```sh
cad3d-ir-parasolid part.x_t converted --step part.step
```

## Package layout

- One root `ProductDefinition` for the file. Without assemblies it is a part
  whose configuration lists every converted body. With assemblies it is an
  assembly whose configuration places each root assembly; one product per
  assembly and one `part` product per body an instance places, with one
  `Occurrence` per instance carrying the stored transform (translation in
  millimetres).
- One `Body` per converted body, named from `SDL/TYSA_NAME` when present,
  with a `brep` representation when `include_brep` is set (one OCCT BREP
  resource per body).
- One package `Visualization` (GLB, millimetres, Z up): one glTF node per
  placement, one mesh per body shared between its placements, one material
  per colour (body colour, face colours by `TopExp::MapShapes` index).

## Diagnostics

| Code | Meaning |
| --- | --- |
| `PARASOLID_EXACT_BREP` | Geometry is the file's own B-Rep converted to OCCT (info) |
| `PARASOLID_UNIT_M` | Lengths were converted from metres to millimetres (info) |
| `PARASOLID_COLOURS_FROM_ATTRIBUTES` | Body and face colours come from `SDL/TYSA_COLOUR` (info) |
| `PARASOLID_ATTRIBUTES_NOT_MAPPED` | Other attributes are not mapped; identifiers listed (info) |
| `PARASOLID_ASSEMBLY_STRUCTURE` | Assemblies and instances became occurrences (info) |
| `PARASOLID_SCHEMA_FAMILY_MATCH` | A Parasolid build without its own profile was read with its schema family (info) |
| `PARASOLID_FACES_DROPPED` | Faces that could not be built were dropped; bodies kept as open shells |
| `PARASOLID_SHAPE_INVALID` | OCCT's BRepCheck rejected the converted shape; shown as built |
| `PARASOLID_BODY_UNSUPPORTED` | Bodies that did not convert, with reasons |
| `PARASOLID_GEOMETRY_UNSUPPORTED` | Geometry records parasolid-kit does not decode |
| `PARASOLID_NEGATIVE_INSTANCE` | Negative (subtracted) instances placed like positive ones |
| `PARASOLID_TRANSFORM_UNSUPPORTED` | General affine or perspective transforms replaced by identity |
| `PARASOLID_ASSEMBLY_TRUNCATED` | Nesting deeper than 64 levels not expanded |
| `PARASOLID_MESH_FAILED` | Bodies that could not be tessellated |
| `PARASOLID_CONVERSION_FAILED` | The bodies could not be converted to OCCT (error) |
| `PARASOLID_LIMIT` | parasolid-kit stopped at a resource limit (error) |
| `PARASOLID_KERNEL_MISSING` | parasolid-kit's occt extra is not installed (error) |
| `PARASOLID_PARSE_FAILED` | The transmit file could not be read (error) |
| `PARASOLID_SCHEMA_UNSUPPORTED` | The schema key is not covered by a built-in profile (error) |
| `PARASOLID_NO_GEOMETRY` | No body yielded a displayable shape (error) |

`statistics` carries the schema key and profile (`schema_coverage` is
`family_inferred` when another Parasolid build of a reviewed schema was
read), the writing application and Parasolid version, body counts by outcome,
dropped faces, assembly and instance counts, the attribute identifiers seen,
triangle counts and the parse and conversion times, so a caller can show what
was and was not read.

## License

MIT, like `cad3d-ir` and parasolid-kit. Parasolid is a trademark of Siemens
Industry Software Inc.; this adapter reads the public transmit format and
ships no Siemens software or schema catalog.
