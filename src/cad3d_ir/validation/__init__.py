"""Public validation API."""

from __future__ import annotations

from pathlib import Path

from cad3d_ir.model import CadPackage
from cad3d_ir.validation.base import IRValidationError, ValidationIssue
from cad3d_ir.validation.graph import validate_graph
from cad3d_ir.validation.resources import validate_resources


def validate_package(
    package: CadPackage,
    *,
    resource_root: str | Path | None = None,
) -> None:
    """Validate the semantic graph and, optionally, resource bytes."""
    validate_graph(package)
    if resource_root is not None:
        validate_resources(package, resource_root)


__all__ = [
    "IRValidationError",
    "ValidationIssue",
    "validate_graph",
    "validate_package",
    "validate_resources",
]
