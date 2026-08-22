"""Common scalar, coordinate, extension, and provenance contracts."""

from __future__ import annotations

import re
from math import isclose, isfinite
from typing import Annotated, Literal, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

Identifier: TypeAlias = Annotated[
    str,
    Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z][A-Za-z0-9_.:-]*$",
    ),
]
Sha256: TypeAlias = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Vector3: TypeAlias = tuple[float, float, float]
Vector4: TypeAlias = tuple[float, float, float, float]
Matrix4: TypeAlias = tuple[Vector4, Vector4, Vector4, Vector4]
PropertyMap: TypeAlias = dict[str, JsonValue]
ExtensionMap: TypeAlias = dict[str, JsonValue]

IDENTITY_MATRIX_4: Matrix4 = (
    (1.0, 0.0, 0.0, 0.0),
    (0.0, 1.0, 0.0, 0.0),
    (0.0, 0.0, 1.0, 0.0),
    (0.0, 0.0, 0.0, 1.0),
)

_EXTENSION_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*(?:\.[a-z0-9][a-z0-9-]*)+$")
_KNOWN_LENGTH_SCALES = {
    "um": 1e-6,
    "mm": 1e-3,
    "cm": 1e-2,
    "m": 1.0,
    "inch": 0.0254,
    "ft": 0.3048,
}


class CoreModel(BaseModel):
    """Strict base model used by every public IR type."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
    )


class ExtensibleModel(CoreModel):
    """Common property and namespaced-extension carrier."""

    properties: PropertyMap = Field(default_factory=dict)
    extensions: ExtensionMap = Field(default_factory=dict)

    @field_validator("properties")
    @classmethod
    def _validate_property_names(cls, value: PropertyMap) -> PropertyMap:
        empty = [key for key in value if not key.strip()]
        if empty:
            raise ValueError("property names must not be empty")
        return value

    @field_validator("extensions")
    @classmethod
    def _validate_extension_names(cls, value: ExtensionMap) -> ExtensionMap:
        invalid = [key for key in value if _EXTENSION_NAME_RE.fullmatch(key) is None]
        if invalid:
            raise ValueError(
                "extension names must use reverse-domain-style lowercase names: "
                + ", ".join(sorted(invalid))
            )
        return value


class SourceRef(CoreModel):
    """Trace one normalized object back to a source document and native item."""

    document_id: Identifier
    source_id: str | None = None
    source_kind: str | None = None
    source_path: str | None = None
    persistent_id: str | None = None
    metadata: PropertyMap = Field(default_factory=dict)


class NamedObject(ExtensibleModel):
    """A globally identified, optionally named manifest object."""

    id: Identifier
    name: str | None = None


class IRObject(NamedObject):
    """A common IR object that may carry native-source provenance."""

    source: SourceRef | None = None


class UnitSystem(CoreModel):
    """Package-wide semantic coordinate units."""

    length_unit: Literal[
        "um",
        "mm",
        "cm",
        "m",
        "inch",
        "ft",
        "custom",
        "unitless",
        "unknown",
    ] = "mm"
    length_scale_to_m: float | None = None
    angle_unit: Literal["rad"] = "rad"

    @model_validator(mode="after")
    def _validate_scale(self) -> UnitSystem:
        scale = self.length_scale_to_m
        if scale is not None and (not isfinite(scale) or scale <= 0):
            raise ValueError("length_scale_to_m must be finite and greater than zero")

        if self.length_unit == "custom" and scale is None:
            raise ValueError("custom length units require length_scale_to_m")

        if self.length_unit in {"unitless", "unknown"} and scale is not None:
            raise ValueError(
                f"{self.length_unit} length units cannot define length_scale_to_m"
            )

        known_scale = _KNOWN_LENGTH_SCALES.get(self.length_unit)
        if (
            known_scale is not None
            and scale is not None
            and not isclose(
                scale,
                known_scale,
                rel_tol=0.0,
                abs_tol=known_scale * 1e-12,
            )
        ):
            raise ValueError(f"length_scale_to_m disagrees with {self.length_unit!r}")
        return self

    @property
    def effective_length_scale_to_m(self) -> float | None:
        """Return the resolved scale, or ``None`` for unknown/unitless units."""
        if self.length_scale_to_m is not None:
            return self.length_scale_to_m
        return _KNOWN_LENGTH_SCALES.get(self.length_unit)


class CoordinateSystem(CoreModel):
    """Canonical package coordinate and matrix conventions."""

    handedness: Literal["right"] = "right"
    up_axis: Literal["z"] = "z"
    matrix_layout: Literal["row-major"] = "row-major"
    vector_convention: Literal["column"] = "column"


class Transform(CoreModel):
    """A finite affine 4x4 transform."""

    matrix: Matrix4 = IDENTITY_MATRIX_4

    @field_validator("matrix")
    @classmethod
    def _validate_matrix(cls, value: Matrix4) -> Matrix4:
        if not all(isfinite(component) for row in value for component in row):
            raise ValueError("transform matrix values must be finite")

        affine_row = value[3]
        expected = (0.0, 0.0, 0.0, 1.0)
        if not all(
            isclose(actual, target, rel_tol=0.0, abs_tol=1e-12)
            for actual, target in zip(affine_row, expected, strict=True)
        ):
            raise ValueError("transform matrix must be affine with row [0, 0, 0, 1]")
        return value


class BBox3(CoreModel):
    """Axis-aligned bounds expressed in package units."""

    min: Vector3
    max: Vector3

    @model_validator(mode="after")
    def _validate_bounds(self) -> BBox3:
        values = (*self.min, *self.max)
        if not all(isfinite(value) for value in values):
            raise ValueError("bounding-box values must be finite")
        if any(lower > upper for lower, upper in zip(self.min, self.max, strict=True)):
            raise ValueError("bounding-box minimum must not exceed maximum")
        return self
