from __future__ import annotations

import pytest

from cad3d_ir import (
    CadPackage,
    Configuration,
    CoordinateSystem,
    IRValidationError,
    Occurrence,
    ProductDefinition,
    RootSelection,
    UnitSystem,
    validate_package,
)


def _package_fields() -> dict[str, object]:
    return {
        "format": "cad3d-ir",
        "version": "0.1.0",
        "units": UnitSystem(length_unit="mm"),
        "coordinate_system": CoordinateSystem(),
    }


def test_sample_graph_is_valid(sample_package: CadPackage) -> None:
    validate_package(sample_package)


def test_dangling_representation_is_rejected(sample_package: CadPackage) -> None:
    package = sample_package.model_copy(deep=True)
    package.bodies[0].representation_ids = ["representation-missing"]

    with pytest.raises(IRValidationError, match="DANGLING_REPRESENTATION_REFERENCE"):
        validate_package(package)


def test_ids_are_global(sample_package: CadPackage) -> None:
    package = sample_package.model_copy(deep=True)
    package.materials[0].id = package.bodies[0].id

    with pytest.raises(IRValidationError, match="DUPLICATE_ID"):
        validate_package(package)


def test_present_extension_must_be_declared(sample_package: CadPackage) -> None:
    package = sample_package.model_copy(deep=True)
    package.extensions_used = []

    with pytest.raises(IRValidationError, match="UNDECLARED_EXTENSION"):
        validate_package(package)


def test_representation_derivation_cycle_is_rejected(
    sample_package: CadPackage,
) -> None:
    package = sample_package.model_copy(deep=True)
    package.representations[
        0
    ].derivation.source_representation_id = package.representations[0].id

    with pytest.raises(
        IRValidationError,
        match="REPRESENTATION_DERIVATION_CYCLE",
    ):
        validate_package(package)


def test_recursive_configuration_graph_is_rejected() -> None:
    product_a = ProductDefinition(
        id="product-a",
        kind="assembly",
        default_configuration_id="configuration-a",
    )
    product_b = ProductDefinition(
        id="product-b",
        kind="assembly",
        default_configuration_id="configuration-b",
    )
    occurrence_b = Occurrence(
        id="occurrence-b",
        product_id=product_b.id,
        configuration_id="configuration-b",
    )
    occurrence_a = Occurrence(
        id="occurrence-a",
        product_id=product_a.id,
        configuration_id="configuration-a",
    )
    configuration_a = Configuration(
        id="configuration-a",
        product_id=product_a.id,
        occurrence_ids=[occurrence_b.id],
    )
    configuration_b = Configuration(
        id="configuration-b",
        product_id=product_b.id,
        occurrence_ids=[occurrence_a.id],
    )
    package = CadPackage(
        **_package_fields(),
        id="package-cycle",
        product_definitions=[product_a, product_b],
        configurations=[configuration_a, configuration_b],
        occurrences=[occurrence_a, occurrence_b],
        roots=[
            RootSelection(
                product_id=product_a.id,
                configuration_id=configuration_a.id,
            )
        ],
    )

    with pytest.raises(IRValidationError, match="CONFIGURATION_CYCLE"):
        validate_package(package)


def test_occurrence_has_one_snapshot_owner() -> None:
    product = ProductDefinition(id="product-shared", kind="part")
    occurrence = Occurrence(id="occurrence-shared", product_id=product.id)
    owner_a = Configuration(
        id="configuration-owner-a",
        product_id=product.id,
        occurrence_ids=[occurrence.id],
    )
    owner_b = Configuration(
        id="configuration-owner-b",
        product_id=product.id,
        occurrence_ids=[occurrence.id],
    )
    package = CadPackage(
        **_package_fields(),
        id="package-shared",
        product_definitions=[product],
        configurations=[owner_a, owner_b],
        occurrences=[occurrence],
    )

    with pytest.raises(IRValidationError, match="MULTIPLE_OCCURRENCE_OWNERS"):
        validate_package(package)
