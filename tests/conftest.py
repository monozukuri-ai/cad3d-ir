from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest

from cad3d_ir import (
    Appearance,
    BBox3,
    Body,
    CadPackage,
    Configuration,
    GeometryRepresentation,
    Material,
    ProductDefinition,
    RepresentationDerivation,
    Resource,
    RootSelection,
    SourceDocument,
    SourceRef,
    UnitSystem,
)


@pytest.fixture
def resource_bytes() -> bytes:
    return b"ISO-10303-21;\nHEADER;\nENDSEC;\nEND-ISO-10303-21;\n"


@pytest.fixture
def sample_package(resource_bytes: bytes) -> CadPackage:
    digest = sha256(resource_bytes).hexdigest()
    source = SourceDocument(
        id="source-step",
        name="bracket.step",
        format="step-ap242",
        uri="source/bracket.step",
        sha256=digest,
    )
    source_ref = SourceRef(
        document_id=source.id,
        source_id="#42",
        source_kind="advanced_brep_shape_representation",
    )
    resource = Resource(
        id="resource-brep",
        uri="geometry/bracket.step",
        media_type="model/step",
        encoding="step-ap242",
        sha256=digest,
        byte_length=len(resource_bytes),
        source=source_ref,
    )
    representation = GeometryRepresentation(
        id="representation-brep",
        name="Design B-Rep",
        kind="brep",
        role="design",
        fidelity="exact",
        resource_id=resource.id,
        units=UnitSystem(length_unit="mm"),
        bounds=BBox3(min=(0.0, 0.0, 0.0), max=(10.0, 20.0, 5.0)),
        derivation=RepresentationDerivation(
            relation="converted",
            method="reference-fixture",
            source=source_ref,
        ),
        source=source_ref,
    )
    material = Material(
        id="material-steel",
        name="Steel",
        density_kg_m3=7850.0,
    )
    appearance = Appearance(
        id="appearance-gray",
        name="Gray",
        base_color=(0.5, 0.5, 0.5, 1.0),
    )
    body = Body(
        id="body-bracket",
        name="Bracket body",
        representation_ids=[representation.id],
        material_id=material.id,
        appearance_id=appearance.id,
        source=source_ref,
    )
    configuration = Configuration(
        id="configuration-default",
        name="Default",
        product_id="product-bracket",
        body_ids=[body.id],
        source=source_ref,
    )
    product = ProductDefinition(
        id="product-bracket",
        name="Bracket",
        kind="part",
        source_document_id=source.id,
        default_configuration_id=configuration.id,
        source=source_ref,
        extensions={"org.example.native": {"document_type": "part"}},
    )
    return CadPackage(
        format="cad3d-ir",
        version="0.1.0",
        id="package-bracket",
        name="Bracket fixture",
        units=UnitSystem(length_unit="mm"),
        coordinate_system={
            "handedness": "right",
            "up_axis": "z",
            "matrix_layout": "row-major",
            "vector_convention": "column",
        },
        source_documents=[source],
        product_definitions=[product],
        configurations=[configuration],
        bodies=[body],
        representations=[representation],
        resources=[resource],
        materials=[material],
        appearances=[appearance],
        roots=[
            RootSelection(
                product_id=product.id,
                configuration_id=configuration.id,
            )
        ],
        extensions_used=["org.example.native"],
    )


@pytest.fixture
def package_directory(
    tmp_path: Path,
    resource_bytes: bytes,
) -> Path:
    resource_path = tmp_path / "geometry" / "bracket.step"
    resource_path.parent.mkdir(parents=True)
    resource_path.write_bytes(resource_bytes)
    return tmp_path
