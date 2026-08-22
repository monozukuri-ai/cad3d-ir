# cad3d-ir design

## Purpose

`cad3d-ir` defines a portable semantic contract for exchanging 3D CAD product
data across source formats and geometry ecosystems. It is not a universal
native-CAD model, a geometry kernel, or a modeling API.

The IR normalizes concepts that survive across formats: source documents,
logical products, named configurations, reusable component occurrences,
transforms, bodies, alternate geometry representations, material and
appearance assignments, provenance, and explicit conversion loss.

Format-specific concepts remain in the source parser's typed model until an
adapter maps them into the common IR. Concepts without a safe common meaning
are retained under namespaced extensions or reported as unsupported; they are
never silently relabeled as common semantics.

## Boundary

The core distribution owns:

- typed models and the versioned manifest schema;
- identifier, reference, unit, coordinate, and transform conventions;
- deterministic manifest serialization and resource addressing;
- structural, graph, and resource-integrity validation;
- provenance, conversion diagnostics, and adapter protocols.

The core distribution does not own:

- STEP, SOLIDWORKS, JT, 3DXML, IFC, Parasolid, or ACIS parsing;
- an Open CASCADE or other kernel runtime;
- tessellation, healing, feature recognition, mass calculation, or rendering;
- project-directory crawling, PDM lookup, network resolution, or Web upload;
- 3D-to-2D projection or the 2D drawing entity model.

Those capabilities may be first-party packages, but they must consume or
produce the public IR contract rather than becoming implicit core behavior.

## Model boundaries

Physical source files and logical products are separate. One source file can
contain multiple products, and one logical product can be assembled from
multiple files. `SourceDocument` therefore records provenance and dependencies,
while `ProductDefinition` records reusable CAD meaning.

Product definitions and occurrences are also separate. Geometry belongs to a
definition/configuration; placement, visibility, suppression state, and the
selected child configuration belong to an occurrence. Repeated components do
not duplicate body geometry.

A `Configuration` is a named product state. A `GeometryRepresentation` is an
encoding or fidelity choice such as exact B-Rep, tessellation, or wireframe.
They are intentionally distinct: one configuration can have multiple
representations, and one representation can be reused where its geometry is
identical.

Configurations are serialized as resolved snapshots in schema `0.1.x`.
Each configuration directly lists its bodies and child occurrences. Delta or
inheritance semantics remain source-specific until more than one format proves
a safe common contract.

## Geometry resources

The manifest never serializes live `TopoDS_Shape`, `cq.Shape`, vendor-kernel
handles, or a custom JSON B-Rep. A body points to one or more typed
representations, and each representation points to a content-addressed binary
resource.

Resources use package-relative POSIX paths and record their byte length and
SHA-256 digest. A development package can be a directory containing
`manifest.json`; a later archive profile can package the same layout without
changing manifest semantics.

The representation records whether geometry is native, converted,
tessellated, approximated, or repaired. Kernel-specific encodings are allowed,
but do not become core dependencies. Portable exchange profiles can require a
standard B-Rep resource while local profiles can additionally use a faster
kernel-native resource.

Topology references are scoped to a geometry representation and its resource
digest. A local face or edge identifier is not promised to survive re-import,
healing, or a kernel-version change unless the source adapter provides a
separate persistent source identifier.

## Coordinates and units

Schema `0.1.x` uses right-handed, Z-up coordinates. Matrices are serialized in
row-major order and act on column vectors. Occurrence transforms must be affine;
their final row is `[0, 0, 0, 1]`. Reflection and non-uniform scale remain
representable.

All semantic coordinates and transforms in one package share the declared
package unit system. Source units and source coordinate conventions belong in
provenance. Unknown and unitless inputs stay explicit rather than being guessed.

Appearance color channels use non-premultiplied linear sRGB. Source adapters
must convert encoded sRGB or source-specific color spaces before assigning
`Appearance.base_color`.

## Extensions

Extension keys use reverse-domain-style names to avoid collisions. A manifest
declares `extensions_used` and `extensions_required`. Consumers may ignore an
optional extension, but must reject a manifest when they do not understand a
required extension.

Vendor feature trees, equations, mates, design tables, and parser-native
records are extension candidates. A generic feature is added to the common
schema only after multiple independent source formats demonstrate compatible
semantics.

## Validation and partial import

Validation has three independent layers:

1. model/schema validation checks local types and numeric constraints;
2. graph validation checks global IDs, references, ownership, and recursive
   configuration cycles;
3. optional backend validation checks resource bytes and kernel geometry.

Importers return the package together with stable diagnostics and statistics.
The caller supplies a confined `ResourceWriter`, so an adapter can stream large
B-Rep or mesh payloads without holding them in memory or choosing an implicit
filesystem destination.
Partial import is allowed only when each skip, approximation, repair,
normalization, and unresolved reference is explicit. The manifest itself is not
used as a dumping ground for transient log messages.

## Versioning and determinism

The Python distribution version and manifest schema version are independent.
Schema versions use semantic versioning: patches clarify or tighten equivalent
behavior, minors add backward-compatible fields or variants, and majors change
existing meaning or required structure.

For the same validated model and serialization options, manifest output is
byte-identical within a package minor version. Object-list order is preserved;
JSON object keys are canonicalized. Binary resources are addressed by digest.

## Staged scope

Schema `0.1.x` covers the multi-document product graph, resolved
configurations, occurrences, bodies, geometry resources, basic material and
appearance assignments, provenance, and extensions.

PMI/GD&T follows only after the topology-reference contract has real STEP and
native-CAD fixtures. Feature history, sketches, constraints, and assembly mates
remain extensions. A future project envelope may relate `cad3d-ir` products to
`cad2d-ir` drawing views without merging both entity schemas into this package.

The first abstraction gate maps a self-generated STEP AP242/XDE assembly and an
independent typed synthetic native-CAD source fixture to the same semantic
projection. This validates the initial product/configuration/occurrence/body
boundary but is not evidence of real native-format parsing. The next gate is to
run the same contract against an actual native CAD parser fixture. Passing only
a SOLIDWORKS-shaped fixture is not evidence that the common model is
format-neutral.
