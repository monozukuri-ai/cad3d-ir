# cad3d-ir

`cad3d-ir` is a typed, kernel-neutral intermediate representation for 3D CAD
product data.

The core package represents source documents, reusable product definitions,
configurations, component occurrences, bodies, geometry-resource references,
materials, appearances, provenance, and namespaced source extensions. Exact
B-Rep and mesh bytes remain external resources rather than Python or JSON
objects embedded in the manifest.

The core intentionally does not depend on Open CASCADE, CadQuery, a vendor SDK,
or a viewer. Format parsers and geometry backends integrate through separate
adapter packages.

## Status

The `0.1.0` foundation provides:

- typed Pydantic models and a generated JSON Schema;
- graph validation for IDs and cross-object references;
- deterministic manifest JSON serialization;
- atomic, content-addressed directory resource writing;
- optional resource size and SHA-256 validation;
- structured importer diagnostics and a format-adapter protocol;
- a `cad3d-ir validate` CLI.

No CAD format importer is included in the core distribution. This repository's
independently packaged `cad3d-ir-step` workspace member provides the STEP
AP242/XDE reference adapter and abstraction fixture.

## Development

```bash
uv sync --dev
uv run pytest
uv run ruff check .
uv run python scripts/sync_schema.py --check

# STEP adapter
uv run --package cad3d-ir-step pytest packages/cad3d-ir-step/tests
uv run --package cad3d-ir-step cad3d-ir-step model.step output/
```

## Minimal use

```python
from cad3d_ir import (
    CadPackage,
    CoordinateSystem,
    UnitSystem,
    dump_manifest,
    load_manifest,
)

package = CadPackage(
    format="cad3d-ir",
    version="0.1.0",
    id="package-example",
    units=UnitSystem(length_unit="mm"),
    coordinate_system=CoordinateSystem(),
)

dump_manifest(package, "manifest.json", pretty=True)
loaded = load_manifest("manifest.json")
assert loaded == package
```

See [the design document](docs/design.md) for the package boundary, model
invariants, geometry-resource strategy, and staged scope. The reference adapter
and abstraction gate are described in
[the adapter design note](docs/reference-adapters.md).

## License

MIT
