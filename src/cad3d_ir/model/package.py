"""Top-level 3D CAD manifest model."""

from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from pydantic import Field, JsonValue, model_validator

from cad3d_ir.model.common import (
    CoordinateSystem,
    ExtensibleModel,
    Identifier,
    UnitSystem,
)
from cad3d_ir.model.geometry import (
    Appearance,
    Body,
    GeometryRepresentation,
    Material,
    Resource,
)
from cad3d_ir.model.product import (
    Configuration,
    Occurrence,
    ProductDefinition,
    RootSelection,
)
from cad3d_ir.model.source import SourceDocument

SchemaVersion: TypeAlias = Annotated[
    str,
    Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$"),
]
ExtensionName: TypeAlias = Annotated[
    str,
    Field(pattern=r"^[a-z0-9][a-z0-9-]*(?:\.[a-z0-9][a-z0-9-]*)+$"),
]


class CadPackage(ExtensibleModel):
    """A complete `cad3d-ir` manifest without embedded resource bytes."""

    format: Literal["cad3d-ir"]
    version: SchemaVersion
    id: Identifier
    name: str | None = None
    units: UnitSystem
    coordinate_system: CoordinateSystem

    source_documents: list[SourceDocument] = Field(default_factory=list)
    product_definitions: list[ProductDefinition] = Field(default_factory=list)
    configurations: list[Configuration] = Field(default_factory=list)
    occurrences: list[Occurrence] = Field(default_factory=list)
    bodies: list[Body] = Field(default_factory=list)
    representations: list[GeometryRepresentation] = Field(default_factory=list)
    resources: list[Resource] = Field(default_factory=list)
    materials: list[Material] = Field(default_factory=list)
    appearances: list[Appearance] = Field(default_factory=list)
    roots: list[RootSelection] = Field(default_factory=list)

    extensions_used: list[ExtensionName] = Field(default_factory=list)
    extensions_required: list[ExtensionName] = Field(default_factory=list)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_extension_declarations(self) -> CadPackage:
        used = set(self.extensions_used)
        required = set(self.extensions_required)
        if len(used) != len(self.extensions_used):
            raise ValueError("extensions_used entries must be unique")
        if len(required) != len(self.extensions_required):
            raise ValueError("extensions_required entries must be unique")
        undeclared = required - used
        if undeclared:
            raise ValueError(
                "required extensions must also appear in extensions_used: "
                + ", ".join(sorted(undeclared))
            )
        return self
