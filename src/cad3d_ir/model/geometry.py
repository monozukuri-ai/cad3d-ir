"""Bodies, representations, binary resources, and visual assignments."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Annotated, Literal, TypeAlias

from pydantic import Field, JsonValue, field_validator

from cad3d_ir.model.common import (
    BBox3,
    ExtensibleModel,
    Identifier,
    IRObject,
    Sha256,
    SourceRef,
    UnitSystem,
)

ColorChannel: TypeAlias = Annotated[float, Field(ge=0.0, le=1.0)]
ColorRGBA: TypeAlias = Annotated[
    tuple[
        ColorChannel,
        ColorChannel,
        ColorChannel,
        ColorChannel,
    ],
    Field(
        description=(
            "Non-premultiplied linear-sRGB red, green, blue, and alpha channels."
        )
    ),
]


def validate_resource_uri(value: str) -> str:
    """Validate and return one confined package-relative resource URI."""
    if "\\" in value:
        raise ValueError("resource URI must use POSIX separators")
    path = PurePosixPath(value)
    if path.is_absolute() or value in {"", "."}:
        raise ValueError("resource URI must be package-relative")
    if any(part in {"", ".", ".."} for part in value.split("/")):
        raise ValueError("resource URI must not contain empty, dot, or parent parts")
    if "://" in value:
        raise ValueError("resource URI must not be an external URL")
    return value


class Resource(IRObject):
    """A content-addressed binary resource stored beside the manifest."""

    uri: str = Field(min_length=1)
    media_type: str = Field(min_length=1)
    encoding: str = Field(min_length=1)
    sha256: Sha256
    byte_length: int = Field(ge=0)

    @field_validator("uri")
    @classmethod
    def _validate_package_relative_uri(cls, value: str) -> str:
        return validate_resource_uri(value)


class RepresentationDerivation(ExtensibleModel):
    """Describe how one geometry representation was produced."""

    relation: Literal[
        "native",
        "converted",
        "tessellated",
        "approximated",
        "repaired",
    ]
    source_representation_id: Identifier | None = None
    method: str | None = None
    tolerance: float | None = Field(default=None, ge=0.0)
    source: SourceRef | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class GeometryRepresentation(IRObject):
    """One exact or derived representation of a body."""

    kind: Literal["brep", "mesh", "wireframe", "point-cloud"]
    role: Literal["design", "visualization", "collision", "preview"] = "design"
    fidelity: Literal["exact", "derived", "approximate"]
    resource_id: Identifier
    units: UnitSystem | None = None
    bounds: BBox3 | None = None
    derivation: RepresentationDerivation | None = None
    topology_namespace: Identifier | None = None


class Body(IRObject):
    """A reusable 3D body with zero or more geometry encodings."""

    representation_ids: list[Identifier] = Field(default_factory=list)
    material_id: Identifier | None = None
    appearance_id: Identifier | None = None


class Material(IRObject):
    """A minimal physical-material assignment."""

    density_kg_m3: float | None = Field(default=None, gt=0.0)


class Appearance(IRObject):
    """A minimal display appearance independent of a renderer."""

    base_color: ColorRGBA | None = None
    double_sided: bool | None = None
