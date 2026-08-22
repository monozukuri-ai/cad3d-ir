"""Cross-object graph validation beyond expressible JSON Schema constraints."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel

from cad3d_ir.model import CadPackage
from cad3d_ir.model.common import IRObject, SourceRef
from cad3d_ir.validation.base import IRValidationError, ValidationIssue


def _duplicates(values: Iterable[str]) -> set[str]:
    counts = Counter(values)
    return {value for value, count in counts.items() if count > 1}


def _add_duplicate_reference_issues(
    issues: list[ValidationIssue],
    *,
    values: list[str],
    path: str,
) -> None:
    for value in sorted(_duplicates(values)):
        issues.append(
            ValidationIssue(
                code="DUPLICATE_REFERENCE",
                path=path,
                message=f"reference {value!r} appears more than once",
            )
        )


def _iter_models(value: Any) -> Iterable[BaseModel]:
    if isinstance(value, BaseModel):
        yield value
        for field_name in type(value).model_fields:
            yield from _iter_models(getattr(value, field_name))
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _iter_models(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_models(item)


def _validate_source_ref(
    issues: list[ValidationIssue],
    source: SourceRef | None,
    source_document_ids: set[str],
    path: str,
) -> None:
    if source is not None and source.document_id not in source_document_ids:
        issues.append(
            ValidationIssue(
                code="DANGLING_SOURCE_REFERENCE",
                path=f"{path}.source.document_id",
                message=f"unknown source document {source.document_id!r}",
            )
        )


def validate_graph(package: CadPackage) -> None:
    """Validate IDs, references, ownership, extensions, and assembly cycles."""
    issues: list[ValidationIssue] = []

    indexed_collections: list[tuple[str, list[Any]]] = [
        ("source_documents", package.source_documents),
        ("product_definitions", package.product_definitions),
        ("configurations", package.configurations),
        ("occurrences", package.occurrences),
        ("bodies", package.bodies),
        ("representations", package.representations),
        ("resources", package.resources),
        ("materials", package.materials),
        ("appearances", package.appearances),
    ]

    id_locations: dict[str, str] = {package.id: "id"}
    for collection_name, objects in indexed_collections:
        for index, item in enumerate(objects):
            path = f"{collection_name}[{index}].id"
            previous = id_locations.get(item.id)
            if previous is not None:
                issues.append(
                    ValidationIssue(
                        code="DUPLICATE_ID",
                        path=path,
                        message=f"ID {item.id!r} is already used at {previous}",
                    )
                )
            else:
                id_locations[item.id] = path

    for document_index, document in enumerate(package.source_documents):
        for dependency_index, dependency in enumerate(document.dependencies):
            path = (
                f"source_documents[{document_index}].dependencies"
                f"[{dependency_index}].id"
            )
            previous = id_locations.get(dependency.id)
            if previous is not None:
                issues.append(
                    ValidationIssue(
                        code="DUPLICATE_ID",
                        path=path,
                        message=f"ID {dependency.id!r} is already used at {previous}",
                    )
                )
            else:
                id_locations[dependency.id] = path

    source_documents = {item.id: item for item in package.source_documents}
    products = {item.id: item for item in package.product_definitions}
    configurations = {item.id: item for item in package.configurations}
    occurrences = {item.id: item for item in package.occurrences}
    bodies = {item.id: item for item in package.bodies}
    representations = {item.id: item for item in package.representations}
    resources = {item.id: item for item in package.resources}
    materials = {item.id: item for item in package.materials}
    appearances = {item.id: item for item in package.appearances}
    source_document_ids = set(source_documents)

    for document_index, document in enumerate(package.source_documents):
        _add_duplicate_reference_issues(
            issues,
            values=[dependency.id for dependency in document.dependencies],
            path=f"source_documents[{document_index}].dependencies",
        )
        for dependency_index, dependency in enumerate(document.dependencies):
            target_id = dependency.target_document_id
            if target_id is not None and target_id not in source_documents:
                issues.append(
                    ValidationIssue(
                        code="DANGLING_DOCUMENT_REFERENCE",
                        path=(
                            f"source_documents[{document_index}].dependencies"
                            f"[{dependency_index}].target_document_id"
                        ),
                        message=f"unknown source document {target_id!r}",
                    )
                )

    for product_index, product in enumerate(package.product_definitions):
        path = f"product_definitions[{product_index}]"
        _validate_source_ref(issues, product.source, source_document_ids, path)
        if (
            product.source_document_id is not None
            and product.source_document_id not in source_documents
        ):
            issues.append(
                ValidationIssue(
                    code="DANGLING_DOCUMENT_REFERENCE",
                    path=f"{path}.source_document_id",
                    message=f"unknown source document {product.source_document_id!r}",
                )
            )
        default_id = product.default_configuration_id
        if default_id is not None:
            default = configurations.get(default_id)
            if default is None:
                issues.append(
                    ValidationIssue(
                        code="DANGLING_CONFIGURATION_REFERENCE",
                        path=f"{path}.default_configuration_id",
                        message=f"unknown configuration {default_id!r}",
                    )
                )
            elif default.product_id != product.id:
                issues.append(
                    ValidationIssue(
                        code="CONFIGURATION_PRODUCT_MISMATCH",
                        path=f"{path}.default_configuration_id",
                        message=(
                            f"configuration {default_id!r} belongs to "
                            f"product {default.product_id!r}"
                        ),
                    )
                )

    occurrence_owners: dict[str, list[str]] = defaultdict(list)
    for configuration_index, configuration in enumerate(package.configurations):
        path = f"configurations[{configuration_index}]"
        _validate_source_ref(issues, configuration.source, source_document_ids, path)
        if configuration.product_id not in products:
            issues.append(
                ValidationIssue(
                    code="DANGLING_PRODUCT_REFERENCE",
                    path=f"{path}.product_id",
                    message=f"unknown product {configuration.product_id!r}",
                )
            )
        _add_duplicate_reference_issues(
            issues,
            values=configuration.body_ids,
            path=f"{path}.body_ids",
        )
        _add_duplicate_reference_issues(
            issues,
            values=configuration.occurrence_ids,
            path=f"{path}.occurrence_ids",
        )
        for body_index, body_id in enumerate(configuration.body_ids):
            if body_id not in bodies:
                issues.append(
                    ValidationIssue(
                        code="DANGLING_BODY_REFERENCE",
                        path=f"{path}.body_ids[{body_index}]",
                        message=f"unknown body {body_id!r}",
                    )
                )
        for occurrence_index, occurrence_id in enumerate(configuration.occurrence_ids):
            occurrence_owners[occurrence_id].append(configuration.id)
            if occurrence_id not in occurrences:
                issues.append(
                    ValidationIssue(
                        code="DANGLING_OCCURRENCE_REFERENCE",
                        path=f"{path}.occurrence_ids[{occurrence_index}]",
                        message=f"unknown occurrence {occurrence_id!r}",
                    )
                )

    for occurrence_index, occurrence in enumerate(package.occurrences):
        path = f"occurrences[{occurrence_index}]"
        _validate_source_ref(issues, occurrence.source, source_document_ids, path)
        product = products.get(occurrence.product_id)
        if product is None:
            issues.append(
                ValidationIssue(
                    code="DANGLING_PRODUCT_REFERENCE",
                    path=f"{path}.product_id",
                    message=f"unknown product {occurrence.product_id!r}",
                )
            )
        selected_id = occurrence.configuration_id
        if selected_id is not None:
            selected = configurations.get(selected_id)
            if selected is None:
                issues.append(
                    ValidationIssue(
                        code="DANGLING_CONFIGURATION_REFERENCE",
                        path=f"{path}.configuration_id",
                        message=f"unknown configuration {selected_id!r}",
                    )
                )
            elif selected.product_id != occurrence.product_id:
                issues.append(
                    ValidationIssue(
                        code="CONFIGURATION_PRODUCT_MISMATCH",
                        path=f"{path}.configuration_id",
                        message=(
                            f"configuration {selected_id!r} belongs to "
                            f"product {selected.product_id!r}"
                        ),
                    )
                )

        owners = occurrence_owners.get(occurrence.id, [])
        if not owners:
            issues.append(
                ValidationIssue(
                    code="ORPHAN_OCCURRENCE",
                    path=path,
                    message="occurrence is not owned by any configuration",
                )
            )
        elif len(owners) > 1:
            issues.append(
                ValidationIssue(
                    code="MULTIPLE_OCCURRENCE_OWNERS",
                    path=path,
                    message=(
                        "schema 0.1.x occurrences are resolved snapshots and must "
                        f"have one owner; found {', '.join(owners)}"
                    ),
                )
            )

    for body_index, body in enumerate(package.bodies):
        path = f"bodies[{body_index}]"
        _validate_source_ref(issues, body.source, source_document_ids, path)
        _add_duplicate_reference_issues(
            issues,
            values=body.representation_ids,
            path=f"{path}.representation_ids",
        )
        for representation_index, representation_id in enumerate(
            body.representation_ids
        ):
            if representation_id not in representations:
                issues.append(
                    ValidationIssue(
                        code="DANGLING_REPRESENTATION_REFERENCE",
                        path=f"{path}.representation_ids[{representation_index}]",
                        message=f"unknown representation {representation_id!r}",
                    )
                )
        if body.material_id is not None and body.material_id not in materials:
            issues.append(
                ValidationIssue(
                    code="DANGLING_MATERIAL_REFERENCE",
                    path=f"{path}.material_id",
                    message=f"unknown material {body.material_id!r}",
                )
            )
        if body.appearance_id is not None and body.appearance_id not in appearances:
            issues.append(
                ValidationIssue(
                    code="DANGLING_APPEARANCE_REFERENCE",
                    path=f"{path}.appearance_id",
                    message=f"unknown appearance {body.appearance_id!r}",
                )
            )

    for representation_index, representation in enumerate(package.representations):
        path = f"representations[{representation_index}]"
        _validate_source_ref(issues, representation.source, source_document_ids, path)
        if representation.resource_id not in resources:
            issues.append(
                ValidationIssue(
                    code="DANGLING_RESOURCE_REFERENCE",
                    path=f"{path}.resource_id",
                    message=f"unknown resource {representation.resource_id!r}",
                )
            )
        derivation = representation.derivation
        if derivation is not None:
            _validate_source_ref(
                issues,
                derivation.source,
                source_document_ids,
                f"{path}.derivation",
            )
            source_id = derivation.source_representation_id
            if source_id is not None and source_id not in representations:
                issues.append(
                    ValidationIssue(
                        code="DANGLING_REPRESENTATION_REFERENCE",
                        path=f"{path}.derivation.source_representation_id",
                        message=f"unknown representation {source_id!r}",
                    )
                )

    for collection_name, objects in (
        ("resources", package.resources),
        ("materials", package.materials),
        ("appearances", package.appearances),
    ):
        for index, item in enumerate(objects):
            if isinstance(item, IRObject):
                _validate_source_ref(
                    issues,
                    item.source,
                    source_document_ids,
                    f"{collection_name}[{index}]",
                )

    uri_locations: dict[str, str] = {}
    for resource_index, resource in enumerate(package.resources):
        path = f"resources[{resource_index}].uri"
        previous = uri_locations.get(resource.uri)
        if previous is not None:
            issues.append(
                ValidationIssue(
                    code="DUPLICATE_RESOURCE_URI",
                    path=path,
                    message=(
                        f"resource URI {resource.uri!r} is already used at {previous}"
                    ),
                )
            )
        else:
            uri_locations[resource.uri] = path

    for root_index, root in enumerate(package.roots):
        path = f"roots[{root_index}]"
        product = products.get(root.product_id)
        if product is None:
            issues.append(
                ValidationIssue(
                    code="DANGLING_PRODUCT_REFERENCE",
                    path=f"{path}.product_id",
                    message=f"unknown product {root.product_id!r}",
                )
            )
        if root.configuration_id is not None:
            configuration = configurations.get(root.configuration_id)
            if configuration is None:
                issues.append(
                    ValidationIssue(
                        code="DANGLING_CONFIGURATION_REFERENCE",
                        path=f"{path}.configuration_id",
                        message=f"unknown configuration {root.configuration_id!r}",
                    )
                )
            elif configuration.product_id != root.product_id:
                issues.append(
                    ValidationIssue(
                        code="CONFIGURATION_PRODUCT_MISMATCH",
                        path=f"{path}.configuration_id",
                        message=(
                            f"configuration {root.configuration_id!r} belongs to "
                            f"product {configuration.product_id!r}"
                        ),
                    )
                )

    declared_extensions = set(package.extensions_used)
    actual_extensions: set[str] = set()
    for model in _iter_models(package):
        extensions = getattr(model, "extensions", None)
        if isinstance(extensions, dict):
            actual_extensions.update(extensions)
    for extension_name in sorted(actual_extensions - declared_extensions):
        issues.append(
            ValidationIssue(
                code="UNDECLARED_EXTENSION",
                path="extensions_used",
                message=f"extension {extension_name!r} is present but not declared",
            )
        )

    default_configuration_by_product = {
        product.id: product.default_configuration_id
        for product in package.product_definitions
    }
    configuration_edges: dict[str, list[str]] = defaultdict(list)
    for configuration in package.configurations:
        for occurrence_id in configuration.occurrence_ids:
            occurrence = occurrences.get(occurrence_id)
            if occurrence is None:
                continue
            selected_id = occurrence.configuration_id
            if selected_id is None:
                selected_id = default_configuration_by_product.get(
                    occurrence.product_id
                )
            if selected_id is not None and selected_id in configurations:
                configuration_edges[configuration.id].append(selected_id)

    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(configuration_id: str) -> None:
        state[configuration_id] = 1
        stack.append(configuration_id)
        for child_id in configuration_edges.get(configuration_id, []):
            child_state = state.get(child_id, 0)
            if child_state == 0:
                visit(child_id)
            elif child_state == 1:
                cycle_start = stack.index(child_id)
                cycle = stack[cycle_start:] + [child_id]
                issues.append(
                    ValidationIssue(
                        code="CONFIGURATION_CYCLE",
                        path="configurations",
                        message="recursive configuration graph: " + " -> ".join(cycle),
                    )
                )
        stack.pop()
        state[configuration_id] = 2

    for configuration_id in configurations:
        if state.get(configuration_id, 0) == 0:
            visit(configuration_id)

    representation_edges: dict[str, str] = {}
    for representation in package.representations:
        if (
            representation.derivation is not None
            and representation.derivation.source_representation_id is not None
            and representation.derivation.source_representation_id in representations
        ):
            representation_edges[representation.id] = (
                representation.derivation.source_representation_id
            )

    state.clear()
    stack.clear()

    def visit_representation(representation_id: str) -> None:
        state[representation_id] = 1
        stack.append(representation_id)
        source_id = representation_edges.get(representation_id)
        if source_id is not None:
            source_state = state.get(source_id, 0)
            if source_state == 0:
                visit_representation(source_id)
            elif source_state == 1:
                cycle_start = stack.index(source_id)
                cycle = stack[cycle_start:] + [source_id]
                issues.append(
                    ValidationIssue(
                        code="REPRESENTATION_DERIVATION_CYCLE",
                        path="representations",
                        message="cyclic representation derivation: "
                        + " -> ".join(cycle),
                    )
                )
        stack.pop()
        state[representation_id] = 2

    for representation_id in representations:
        if state.get(representation_id, 0) == 0:
            visit_representation(representation_id)

    if issues:
        raise IRValidationError(issues)
