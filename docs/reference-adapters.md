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
silently discarded. Configuration synthesis and requested-but-unavailable mesh
generation are also explicit diagnostics.

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
