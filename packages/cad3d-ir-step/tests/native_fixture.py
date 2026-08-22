"""Typed synthetic native-CAD source model and independent IR mapper."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import Literal

from cad3d_ir import (
    Appearance,
    BBox3,
    Body,
    CadPackage,
    Configuration,
    CoordinateSystem,
    GeometryRepresentation,
    Occurrence,
    ProductDefinition,
    RepresentationDerivation,
    RootSelection,
    SourceDocument,
    SourceRef,
    Transform,
    UnitSystem,
    validate_package,
)
from cad3d_ir.io import ResourceWriter
from pydantic import BaseModel, ConfigDict, Field, model_validator


class _NativeModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NativeBody(_NativeModel):
    name: str
    bounds_min: tuple[float, float, float]
    bounds_max: tuple[float, float, float]
    base_color: tuple[float, float, float, float]


class NativeOccurrence(_NativeModel):
    key: str
    name: str
    product_key: str
    translation: tuple[float, float, float]


class NativeProduct(_NativeModel):
    key: str
    name: str
    kind: Literal["part", "assembly"]
    body: NativeBody | None = None
    occurrences: list[NativeOccurrence] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_product_shape(self) -> NativeProduct:
        if self.kind == "part" and self.body is None:
            raise ValueError("native fixture parts require a body")
        if self.kind == "assembly" and self.body is not None:
            raise ValueError("native fixture assemblies do not carry a body")
        return self


class NativeDocument(_NativeModel):
    format: Literal["synthetic-native-cad"]
    version: str
    name: str
    units: Literal["mm"]
    root_product_key: str
    products: list[NativeProduct]

    @model_validator(mode="after")
    def _validate_references(self) -> NativeDocument:
        product_keys = [product.key for product in self.products]
        if len(product_keys) != len(set(product_keys)):
            raise ValueError("native fixture product keys must be unique")
        known = set(product_keys)
        if self.root_product_key not in known:
            raise ValueError("native fixture root product is unknown")
        for product in self.products:
            for occurrence in product.occurrences:
                if occurrence.product_key not in known:
                    raise ValueError(
                        f"native occurrence target is unknown: {occurrence.product_key}"
                    )
        return self


def load_native_document(path: Path) -> NativeDocument:
    return NativeDocument.model_validate_json(path.read_bytes())


def _source_ref(source_id: str, key: str, kind: str) -> SourceRef:
    return SourceRef(
        document_id=source_id,
        source_id=key,
        source_kind=kind,
    )


def _translation_matrix(
    translation: tuple[float, float, float],
) -> tuple[
    tuple[float, float, float, float],
    tuple[float, float, float, float],
    tuple[float, float, float, float],
    tuple[float, float, float, float],
]:
    x, y, z = translation
    return (
        (1.0, 0.0, 0.0, x),
        (0.0, 1.0, 0.0, y),
        (0.0, 0.0, 1.0, z),
        (0.0, 0.0, 0.0, 1.0),
    )


def map_native_document(
    path: Path,
    *,
    resources: ResourceWriter,
) -> CadPackage:
    """Map the native test model without using any STEP adapter code."""
    data = path.read_bytes()
    digest = sha256(data).hexdigest()
    document = NativeDocument.model_validate_json(data)
    source_id = f"source-native-{digest[:12]}"
    source = SourceDocument(
        id=source_id,
        name=path.name,
        format=document.format,
        version=document.version,
        uri=path.name,
        sha256=digest,
    )

    product_ids = {
        product.key: f"native-product-{product.key}" for product in document.products
    }
    configuration_ids = {
        product.key: f"native-configuration-{product.key}"
        for product in document.products
    }
    occurrence_ids_by_owner: dict[str, list[str]] = {
        product.key: [] for product in document.products
    }

    occurrences: list[Occurrence] = []
    for product in document.products:
        for native_occurrence in product.occurrences:
            occurrence_id = f"native-occurrence-{native_occurrence.key}"
            occurrence_ids_by_owner[product.key].append(occurrence_id)
            occurrences.append(
                Occurrence(
                    id=occurrence_id,
                    name=native_occurrence.name,
                    product_id=product_ids[native_occurrence.product_key],
                    configuration_id=configuration_ids[native_occurrence.product_key],
                    transform=Transform(
                        matrix=_translation_matrix(native_occurrence.translation)
                    ),
                    visible=True,
                    source=_source_ref(
                        source_id,
                        native_occurrence.key,
                        "native-occurrence",
                    ),
                )
            )

    bodies: list[Body] = []
    representations: list[GeometryRepresentation] = []
    resource_records = []
    appearances: list[Appearance] = []
    body_ids_by_product: dict[str, list[str]] = {
        product.key: [] for product in document.products
    }
    for product in document.products:
        if product.body is None:
            continue
        body = product.body
        source_ref = _source_ref(source_id, product.key, "native-part")
        body_id = f"native-body-{product.key}"
        representation_id = f"native-representation-{product.key}"
        resource_id = f"native-resource-{product.key}"
        appearance_id = f"native-appearance-{product.key}"
        payload = f"synthetic-native-brep-v1:{product.key}\n".encode()
        resource = resources.write_chunks(
            id=resource_id,
            name=f"{product.name} native B-Rep",
            uri=f"geometry/{product.key}.native-brep",
            chunks=(payload,),
            media_type="application/vnd.example.native-brep",
            encoding="synthetic-native-brep-v1",
            source=source_ref,
        )
        resource_records.append(resource)
        appearances.append(
            Appearance(
                id=appearance_id,
                name=f"{product.name} appearance",
                base_color=body.base_color,
                source=source_ref,
            )
        )
        representations.append(
            GeometryRepresentation(
                id=representation_id,
                name=f"{product.name} design B-Rep",
                kind="brep",
                role="design",
                fidelity="exact",
                resource_id=resource_id,
                units=UnitSystem(length_unit="mm"),
                bounds=BBox3(min=body.bounds_min, max=body.bounds_max),
                derivation=RepresentationDerivation(
                    relation="native",
                    method="synthetic native fixture mapper",
                    source=source_ref,
                ),
                topology_namespace=f"native-topology-{product.key}",
                source=source_ref,
            )
        )
        bodies.append(
            Body(
                id=body_id,
                name=body.name,
                representation_ids=[representation_id],
                appearance_id=appearance_id,
                source=source_ref,
            )
        )
        body_ids_by_product[product.key].append(body_id)

    products: list[ProductDefinition] = []
    configurations: list[Configuration] = []
    for product in document.products:
        source_ref = _source_ref(
            source_id,
            product.key,
            f"native-{product.kind}",
        )
        products.append(
            ProductDefinition(
                id=product_ids[product.key],
                name=product.name,
                kind=product.kind,
                source_document_id=source_id,
                default_configuration_id=configuration_ids[product.key],
                source=source_ref,
            )
        )
        configurations.append(
            Configuration(
                id=configuration_ids[product.key],
                name="Default",
                product_id=product_ids[product.key],
                body_ids=body_ids_by_product[product.key],
                occurrence_ids=occurrence_ids_by_owner[product.key],
                source=source_ref,
            )
        )

    package = CadPackage(
        format="cad3d-ir",
        version="0.1.0",
        id=f"package-native-{digest[:12]}",
        name=document.name,
        units=UnitSystem(length_unit=document.units),
        coordinate_system=CoordinateSystem(),
        source_documents=[source],
        product_definitions=products,
        configurations=configurations,
        occurrences=occurrences,
        bodies=bodies,
        representations=representations,
        resources=resource_records,
        appearances=appearances,
        roots=[
            RootSelection(
                product_id=product_ids[document.root_product_key],
                configuration_id=configuration_ids[document.root_product_key],
            )
        ],
        metadata={"adapter": "synthetic-native-fixture"},
    )
    validate_package(package)
    return package
