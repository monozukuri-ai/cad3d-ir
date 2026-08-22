"""Backend-independent semantic projection for abstraction tests."""

from __future__ import annotations

from typing import Any

from cad3d_ir import CadPackage


def _rounded(value: float) -> float:
    return round(value, 6)


def _matrix(matrix: tuple[tuple[float, ...], ...]) -> list[list[float]]:
    return [[_rounded(value) for value in row] for row in matrix]


def semantic_projection(package: CadPackage) -> dict[str, Any]:
    """Project only semantics expected to survive source-format conversion."""
    products = {product.id: product for product in package.product_definitions}
    configurations = {
        configuration.id: configuration for configuration in package.configurations
    }
    occurrences = {occurrence.id: occurrence for occurrence in package.occurrences}
    bodies = {body.id: body for body in package.bodies}
    representations = {
        representation.id: representation for representation in package.representations
    }
    appearances = {appearance.id: appearance for appearance in package.appearances}

    projected_products = []
    for product in package.product_definitions:
        configuration = configurations[product.default_configuration_id]
        projected_bodies = []
        for body_id in configuration.body_ids:
            body = bodies[body_id]
            representation = representations[body.representation_ids[0]]
            appearance = (
                appearances[body.appearance_id]
                if body.appearance_id is not None
                else None
            )
            projected_bodies.append(
                {
                    "name": body.name,
                    "representation": {
                        "kind": representation.kind,
                        "role": representation.role,
                        "fidelity": representation.fidelity,
                        "bounds": (
                            {
                                "min": [
                                    _rounded(value)
                                    for value in representation.bounds.min
                                ],
                                "max": [
                                    _rounded(value)
                                    for value in representation.bounds.max
                                ],
                            }
                            if representation.bounds is not None
                            else None
                        ),
                    },
                    "base_color": (
                        [_rounded(value) for value in appearance.base_color]
                        if appearance is not None and appearance.base_color is not None
                        else None
                    ),
                }
            )

        projected_occurrences = []
        for occurrence_id in configuration.occurrence_ids:
            occurrence = occurrences[occurrence_id]
            projected_occurrences.append(
                {
                    "name": occurrence.name,
                    "product": products[occurrence.product_id].name,
                    "configuration": configurations[occurrence.configuration_id].name,
                    "transform": _matrix(occurrence.transform.matrix),
                    "visible": occurrence.visible,
                    "suppressed": occurrence.suppressed,
                }
            )
        projected_products.append(
            {
                "name": product.name,
                "kind": product.kind,
                "configuration": configuration.name,
                "bodies": sorted(projected_bodies, key=lambda item: item["name"]),
                "occurrences": sorted(
                    projected_occurrences,
                    key=lambda item: item["name"],
                ),
            }
        )

    return {
        "name": package.name,
        "units": package.units.model_dump(mode="json", exclude_none=True),
        "coordinate_system": package.coordinate_system.model_dump(mode="json"),
        "roots": sorted(products[root.product_id].name for root in package.roots),
        "products": sorted(projected_products, key=lambda item: item["name"]),
    }
