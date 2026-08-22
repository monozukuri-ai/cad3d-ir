# cad3d-ir-step

`cad3d-ir-step` is the STEP AP242/XDE reference importer for `cad3d-ir`.
It uses Open CASCADE XDE to preserve product definitions, assembly occurrences,
local placements, names, and surface colors while exporting each reusable leaf
shape as an external OCCT B-Rep resource.

The runtime uses the official `cadquery-ocp-novtk` binding because conversion
does not require VTK or a visualization stack.

The distribution intentionally depends on `cad3d-ir`; the core distribution
does not depend on this adapter or on Open CASCADE.

## Usage

```python
from pathlib import Path

from cad3d_ir import DirectoryResourceWriter
from cad3d_ir.importers import ImportOptions
from cad3d_ir_step import StepImporter

output = Path("converted")
result = StepImporter().import_file(
    Path("assembly.step"),
    options=ImportOptions(),
    resources=DirectoryResourceWriter(output),
)
```

The command-line entry point writes `manifest.json` and geometry resources:

```console
cad3d-ir-step assembly.step converted/
```

## Current boundary

- AP242 Part 21 files are the strict/default input profile.
- XDE product structure, names, local placements, visibility, and definition
  surface colors are mapped.
- Leaf definition shapes are stored as OCCT B-Rep ASCII v1 resources.
- One resolved `Default` configuration is synthesized for every product.
- PMI/GD&T, layers, physical materials, and external STEP references are not
  yet mapped to common IR fields.

The abstraction test compares a self-generated AP242 assembly with an
independent typed synthetic native-CAD source fixture. It proves the common
product/configuration/occurrence/body abstraction, not SolidWorks parsing.
