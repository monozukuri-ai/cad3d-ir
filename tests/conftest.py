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


# --- GLB fixtures shared by the scene reader and exporter tests ----------------

CUBE_POSITIONS: list[float] = [
    0.0, 0.0, 0.0,
    10.0, 0.0, 0.0,
    10.0, 10.0, 0.0,
    0.0, 10.0, 0.0,
    0.0, 0.0, 10.0,
    10.0, 0.0, 10.0,
    10.0, 10.0, 10.0,
    0.0, 10.0, 10.0,
]  # fmt: skip
# Outward-facing triangles of the six faces (two per quad).
CUBE_INDICES: list[int] = [
    0, 3, 2, 0, 2, 1,
    4, 5, 6, 4, 6, 7,
    0, 1, 5, 0, 5, 4,
    1, 2, 6, 1, 6, 5,
    2, 3, 7, 2, 7, 6,
    3, 0, 4, 3, 4, 7,
]  # fmt: skip
CUBE_EDGES: list[int] = [
    0, 1, 1, 2, 2, 3, 3, 0,
    4, 5, 5, 6, 6, 7, 7, 4,
    0, 4, 1, 5, 2, 6, 3, 7,
]  # fmt: skip
ROT_Z_90 = (
    (0.0, -1.0, 0.0, 0.0),
    (1.0, 0.0, 0.0, 0.0),
    (0.0, 0.0, 1.0, 0.0),
    (0.0, 0.0, 0.0, 1.0),
)


def translation(x: float, y: float, z: float):
    return (
        (1.0, 0.0, 0.0, x),
        (0.0, 1.0, 0.0, y),
        (0.0, 0.0, 1.0, z),
        (0.0, 0.0, 0.0, 1.0),
    )


def build_cubes(*, nested: bool = False, degenerate: bool = False) -> bytes:
    """Two (or three) placements of one 10 mm cube with a shared vertex set.

    ``cube-a`` sits at the origin, ``cube-b`` is moved 20 mm along X, and with
    ``nested`` a ``cube-c`` hangs under ``cube-b`` rotated 90 degrees about Z
    and moved 20 mm along Y. ``degenerate`` adds one zero-area triangle.
    """
    from cad3d_ir.mesh import GlbBuilder, vertex_normals
    from cad3d_ir.mesh.scene import multiply

    builder = GlbBuilder(root_name="fixture", generator="test")
    material = builder.add_material("faces", (0.5, 0.5, 0.5, 1.0))
    mesh = builder.add_mesh("cube", extras={"id": "body-cube"})
    vertex_set = builder.add_vertices(
        positions=CUBE_POSITIONS,
        normals=vertex_normals(CUBE_POSITIONS, CUBE_INDICES),
    )
    indices = list(CUBE_INDICES) + ([0, 0, 1] if degenerate else [])
    builder.add_triangles(
        mesh,
        indices=indices,
        vertices=vertex_set,
        material=material,
        extras={"entityKind": "face", "ranges": [[1, 0, len(indices)]]},
    )
    builder.add_lines(mesh, indices=CUBE_EDGES, vertices=vertex_set)
    builder.add_node("cube-a", mesh=mesh, extras={"id": "occurrence-a"})
    moved = builder.add_node("cube-b", mesh=mesh, matrix=translation(20.0, 0.0, 0.0))
    if nested:
        builder.add_node(
            "cube-c",
            mesh=mesh,
            parent=moved,
            matrix=multiply(translation(0.0, 20.0, 0.0), ROT_Z_90),
        )
    return builder.build()


@pytest.fixture
def cubes_glb() -> bytes:
    return build_cubes()


@pytest.fixture
def nested_cubes_glb() -> bytes:
    return build_cubes(nested=True)


@pytest.fixture
def degenerate_cubes_glb() -> bytes:
    return build_cubes(degenerate=True)
