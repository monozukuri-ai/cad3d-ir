from __future__ import annotations

import pytest
from pydantic import ValidationError

from cad3d_ir import (
    CadPackage,
    CoordinateSystem,
    ProductDefinition,
    Resource,
    Transform,
    UnitSystem,
)


def _package_fields() -> dict[str, object]:
    return {
        "format": "cad3d-ir",
        "version": "0.1.0",
        "units": UnitSystem(length_unit="mm"),
        "coordinate_system": CoordinateSystem(),
    }


def test_custom_units_require_scale() -> None:
    with pytest.raises(ValidationError, match="length_scale_to_m"):
        UnitSystem(length_unit="custom")


def test_known_unit_rejects_disagreeing_scale() -> None:
    with pytest.raises(ValidationError, match="disagrees"):
        UnitSystem(length_unit="mm", length_scale_to_m=1.0)


def test_transform_must_be_affine() -> None:
    with pytest.raises(ValidationError, match="must be affine"):
        Transform(
            matrix=(
                (1.0, 0.0, 0.0, 0.0),
                (0.0, 1.0, 0.0, 0.0),
                (0.0, 0.0, 1.0, 0.0),
                (0.0, 0.0, 1.0, 1.0),
            )
        )


def test_resource_uri_is_package_relative() -> None:
    fields = {
        "id": "resource-test",
        "media_type": "model/step",
        "encoding": "step-ap242",
        "sha256": "0" * 64,
        "byte_length": 0,
    }
    with pytest.raises(ValidationError, match="package-relative"):
        Resource(uri="/tmp/model.step", **fields)
    with pytest.raises(ValidationError, match="parent parts"):
        Resource(uri="geometry/../model.step", **fields)
    with pytest.raises(ValidationError, match="empty, dot"):
        Resource(uri="geometry//model.step", **fields)


def test_extensions_are_namespaced_and_declared() -> None:
    with pytest.raises(ValidationError, match="reverse-domain"):
        ProductDefinition(id="product-test", extensions={"solidworks": {}})

    with pytest.raises(ValidationError, match="also appear"):
        CadPackage(
            **_package_fields(),
            id="package-test",
            extensions_required=["org.example.required"],
        )


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        CadPackage.model_validate(
            {
                **_package_fields(),
                "id": "package-test",
                "unexpected": True,
            }
        )
