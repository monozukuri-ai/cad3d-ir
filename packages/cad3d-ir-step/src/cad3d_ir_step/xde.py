"""Shared OCCT XDE reading and the mapping to the common IR.

Both the STEP and the IGES importer transfer their file into an XCAF document
and hand the resolved product graph to :func:`map_xde`. The mapping is the
same for both; only the reader, the source metadata and the diagnostic code
prefix differ.
"""

from __future__ import annotations

import os
import re
from collections import defaultdict
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from hashlib import sha256
from importlib.metadata import PackageNotFoundError, version
from math import isclose
from pathlib import Path
from tempfile import mkstemp
from typing import Any

from cad3d_ir import (
    Appearance,
    BBox3,
    Body,
    CadPackage,
    Configuration,
    CoordinateSystem,
    GeometryRepresentation,
    ImportDiagnostic,
    Occurrence,
    ProductDefinition,
    RepresentationDerivation,
    RootSelection,
    SourceDocument,
    SourceRef,
    Transform,
    UnitSystem,
    Visualization,
)
from cad3d_ir.constants import CURRENT_IR_VERSION
from cad3d_ir.importers import ImportOptions
from cad3d_ir.io import ResourceWriter
from cad3d_ir.mesh import GLB_ENCODING, GLB_MEDIA_TYPE, GlbBuilder, extend_bounds
from OCP.Bnd import Bnd_Box
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepTools import BRepTools
from OCP.TCollection import TCollection_AsciiString, TCollection_ExtendedString
from OCP.TDataStd import TDataStd_Name
from OCP.TDF import TDF_LabelSequence, TDF_Tool
from OCP.TDocStd import TDocStd_Document
from OCP.XCAFApp import XCAFApp_Application
from OCP.XCAFDoc import XCAFDoc_DocumentTool, XCAFDoc_ShapeTool
from OCP.XCAFPrs import (
    XCAFPrs_DocumentExplorer,
    XCAFPrs_DocumentExplorerFlags_None,
    XCAFPrs_Style,
)

from cad3d_ir_step.errors import StepReadError
from cad3d_ir_step.tessellate import ShapeMesh, linear_deflection_for, mesh_shape

ADAPTER_VERSION = "0.2.0"

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_IDENTITY_3X4 = (
    (1.0, 0.0, 0.0, 0.0),
    (0.0, 1.0, 0.0, 0.0),
    (0.0, 0.0, 1.0, 0.0),
)
_DEFAULT_FACE_COLOR = (0.55, 0.62, 0.70, 1.0)
_EDGE_COLOR = (0.08, 0.10, 0.14, 1.0)

Matrix4 = tuple[
    tuple[float, float, float, float],
    tuple[float, float, float, float],
    tuple[float, float, float, float],
    tuple[float, float, float, float],
]


@dataclass(frozen=True, slots=True)
class XdeNode:
    depth: int
    path: str
    label_entry: str
    definition_entry: str
    definition_label: Any
    definition_name: str
    occurrence_name: str
    is_assembly: bool
    local_matrix: Matrix4
    visible: bool
    color: tuple[float, float, float, float] | None


@dataclass(slots=True)
class XdeReadResult:
    application: Any
    document: Any
    nodes: list[XdeNode]
    source_length_units: tuple[str, ...]
    unsupported_counts: dict[str, int]

    def close(self) -> None:
        """Drop Python references to the transient, non-session XDE document."""
        self.nodes.clear()
        self.document = None
        self.application = None


@dataclass(slots=True)
class MeshSettings:
    """Resolved tessellation request for one import."""

    linear_deflection_ratio: float
    angular_deflection: float
    include_edges: bool
    body_resources: bool


@dataclass(slots=True)
class _MeshOutcome:
    failed_faces: int = 0
    failed_edges: int = 0
    meshed_definitions: int = 0
    empty_definitions: list[str] = field(default_factory=list)


def occt_version() -> str:
    for distribution in ("cadquery-ocp-novtk", "cadquery-ocp"):
        try:
            return version(distribution)
        except PackageNotFoundError:
            continue
    return "unknown"


def hash_file(path: Path) -> tuple[str, int]:
    digest = sha256()
    byte_length = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            byte_length += len(chunk)
    return digest.hexdigest(), byte_length


def _label_entry(label: Any) -> str:
    entry = TCollection_AsciiString()
    TDF_Tool.Entry_s(label, entry)
    return entry.ToCString()


def _label_name(label: Any) -> str | None:
    attribute = TDataStd_Name()
    if not label.FindAttribute(TDataStd_Name.GetID_s(), attribute):
        return None
    value = attribute.Get().ToExtString().strip()
    return value or None


def _matrix_from_location(location: Any) -> Matrix4:
    transform = location.Transformation()
    rows = tuple(
        tuple(float(transform.Value(row, column)) for column in range(1, 5))
        for row in range(1, 4)
    )
    return (*rows, (0.0, 0.0, 0.0, 1.0))  # type: ignore[return-value]


def _style_color(style: Any) -> tuple[float, float, float, float] | None:
    if not style.IsSetColorSurf():
        return None
    rgba = style.GetColorSurfRGBA()
    rgb = rgba.GetRGB()
    return (
        float(rgb.Red()),
        float(rgb.Green()),
        float(rgb.Blue()),
        float(rgba.Alpha()),
    )


def _count_labels(tool: Any, method_name: str) -> int:
    labels = TDF_LabelSequence()
    getattr(tool, method_name)(labels)
    return labels.Length()


def read_xde(
    path: Path,
    *,
    transfer: Callable[[Any, Any], tuple[str, ...]],
) -> XdeReadResult:
    """Transfer ``path`` into a fresh XCAF document and flatten its explorer.

    ``transfer(path, document)`` performs the format-specific read and returns
    the declared source length units (possibly empty).
    """
    application = XCAFApp_Application.GetApplication_s()
    document = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
    application.InitDocument(document)

    source_length_units = transfer(path, document)

    explorer = XCAFPrs_DocumentExplorer(
        document,
        XCAFPrs_DocumentExplorerFlags_None,
        XCAFPrs_Style(),
    )
    nodes: list[XdeNode] = []
    while explorer.More():
        node = explorer.Current()
        definition_entry = _label_entry(node.RefLabel)
        definition_name = _label_name(node.RefLabel) or f"Unnamed {definition_entry}"
        occurrence_name = _label_name(node.Label) or definition_name
        nodes.append(
            XdeNode(
                depth=explorer.CurrentDepth(),
                path=node.Id.ToCString(),
                label_entry=_label_entry(node.Label),
                definition_entry=definition_entry,
                definition_label=node.RefLabel,
                definition_name=definition_name,
                occurrence_name=occurrence_name,
                is_assembly=bool(node.IsAssembly),
                local_matrix=_matrix_from_location(node.LocalTrsf),
                visible=bool(node.Style.IsVisible()),
                color=_style_color(node.Style),
            )
        )
        explorer.Next()

    if not nodes:
        raise StepReadError(f"transfer produced no XDE product roots: {path}")

    layer_tool = XCAFDoc_DocumentTool.LayerTool_s(document.Main())
    dim_tol_tool = XCAFDoc_DocumentTool.DimTolTool_s(document.Main())
    material_tool = XCAFDoc_DocumentTool.MaterialTool_s(document.Main())
    unsupported_counts = {
        "layers": _count_labels(layer_tool, "GetLayerLabels"),
        "dimensions": _count_labels(dim_tol_tool, "GetDimensionLabels"),
        "geometric_tolerances": _count_labels(
            dim_tol_tool,
            "GetGeomToleranceLabels",
        ),
        "datums": _count_labels(dim_tol_tool, "GetDatumLabels"),
        "legacy_dimensional_tolerances": _count_labels(
            dim_tol_tool,
            "GetDimTolLabels",
        ),
        "physical_materials": _count_labels(material_tool, "GetMaterialLabels"),
    }
    return XdeReadResult(
        application=application,
        document=document,
        nodes=nodes,
        source_length_units=source_length_units,
        unsupported_counts=unsupported_counts,
    )


def slug(value: str) -> str:
    text = _SLUG_RE.sub("-", value.casefold()).strip("-")
    return text[:48] or "item"


def make_id(prefix: str, name: str, key: str) -> str:
    suffix = sha256(key.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}-{slug(name)}-{suffix}"


def _source_ref(source_id: str, node: XdeNode, *, definition: bool) -> SourceRef:
    return SourceRef(
        document_id=source_id,
        source_id=node.definition_entry if definition else node.label_entry,
        source_kind="xde-assembly" if node.is_assembly else "xde-simple-shape",
        source_path=node.path,
    )


def _iter_file_chunks(path: Path) -> Iterator[bytes]:
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            yield chunk


def _iter_bytes(payload: bytes, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
    for offset in range(0, len(payload), chunk_size):
        yield payload[offset : offset + chunk_size]


def _write_brep(
    *,
    shape: Any,
    resource_id: str,
    resource_uri: str,
    resource_name: str,
    source: SourceRef,
    resources: ResourceWriter,
    properties: dict[str, Any],
) -> Any:
    descriptor, temporary_name = mkstemp(suffix=".brep")
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        if not BRepTools.Write_s(shape, str(temporary_path)):
            raise StepReadError(f"OCCT failed to serialize B-Rep for {resource_name}")
        return resources.write_chunks(
            id=resource_id,
            name=resource_name,
            uri=resource_uri,
            chunks=_iter_file_chunks(temporary_path),
            media_type="application/vnd.opencascade.brep",
            encoding="occt-brep-ascii-v1",
            source=source,
            properties={"occt_version": occt_version(), **properties},
        )
    finally:
        temporary_path.unlink(missing_ok=True)


def _shape_bounds(shape: Any) -> BBox3 | None:
    """Conservative bounds from surface control points (fast; never too small)."""
    bounds = Bnd_Box()
    BRepBndLib.Add_s(shape, bounds, False)
    if bounds.IsVoid():
        return None
    xmin, ymin, zmin, xmax, ymax, zmax = bounds.Get()
    return BBox3(
        min=(float(xmin), float(ymin), float(zmin)),
        max=(float(xmax), float(ymax), float(zmax)),
    )


def _bbox(bounds: tuple[Any, Any] | None) -> BBox3 | None:
    if bounds is None:
        return None
    return BBox3(min=tuple(bounds[0]), max=tuple(bounds[1]))


def _matmul(left: Matrix4, right: Matrix4) -> Matrix4:
    return tuple(  # type: ignore[return-value]
        tuple(
            sum(left[row][k] * right[k][column] for k in range(4))
            for column in range(4)
        )
        for row in range(4)
    )


def _transform_bounds(
    matrix: Matrix4, bounds: tuple[Any, Any]
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """Return the axis-aligned box of a box's eight corners after ``matrix``."""
    low, high = bounds
    corners = [
        (x, y, z)
        for x in (low[0], high[0])
        for y in (low[1], high[1])
        for z in (low[2], high[2])
    ]
    moved = [
        tuple(
            matrix[row][0] * x
            + matrix[row][1] * y
            + matrix[row][2] * z
            + matrix[row][3]
            for row in range(3)
        )
        for x, y, z in corners
    ]
    return (
        (min(c[0] for c in moved), min(c[1] for c in moved), min(c[2] for c in moved)),
        (max(c[0] for c in moved), max(c[1] for c in moved), max(c[2] for c in moved)),
    )


def _add_shape_mesh(
    builder: GlbBuilder,
    mesh: int,
    shape_mesh: ShapeMesh,
    *,
    face_material: int,
    edge_material: int | None,
) -> None:
    """Append one definition's faces (one primitive) and edges (one primitive)."""
    if shape_mesh.faces:
        positions: list[float] = []
        normals: list[float] = []
        indices: list[int] = []
        ranges: list[list[int]] = []
        for face in shape_mesh.faces:
            offset = len(positions) // 3
            start = len(indices)
            positions.extend(face.positions)
            normals.extend(face.normals)
            indices.extend(index + offset for index in face.indices)
            ranges.append([face.pick_id, start, len(face.indices)])
        builder.add_triangles(
            mesh,
            positions=positions,
            normals=normals,
            indices=indices,
            material=face_material,
            extras={"entityKind": "face", "ranges": ranges},
        )
    if shape_mesh.edges and edge_material is not None:
        positions = []
        indices = []
        ranges = []
        for edge in shape_mesh.edges:
            offset = len(positions) // 3
            start = len(indices)
            positions.extend(edge.positions)
            point_count = len(edge.positions) // 3
            for index in range(point_count - 1):
                indices.extend((offset + index, offset + index + 1))
            ranges.append([edge.pick_id, start, len(indices) - start])
        builder.add_lines(
            mesh,
            positions=positions,
            indices=indices,
            material=edge_material,
            extras={"entityKind": "edge", "ranges": ranges},
        )


def _new_builder(
    name: str,
    *,
    mesh_settings: MeshSettings,
    linear_deflection: float,
    prefix: str,
) -> GlbBuilder:
    return GlbBuilder(
        generator=f"cad3d-ir-step {ADAPTER_VERSION}",
        root_name=name,
        length_unit_to_m=0.001,
        up_axis="z",
        root_extras={
            "cad3d_ir_step": {
                "source_format": prefix.lower(),
                "linear_deflection_mm": linear_deflection,
                "angular_deflection_rad": mesh_settings.angular_deflection,
            }
        },
    )


def map_xde(
    xde: XdeReadResult,
    *,
    source: SourceDocument,
    source_digest: str,
    resources: ResourceWriter,
    diagnostics: list[ImportDiagnostic],
    options: ImportOptions,
    prefix: str,
    resource_properties: dict[str, Any],
    mesh_settings: MeshSettings | None,
) -> CadPackage:
    """Map a flattened XDE graph to products, occurrences, bodies, and resources."""
    first_by_definition: dict[str, XdeNode] = {}
    colors_by_definition: dict[str, set[tuple[float, float, float, float]]] = (
        defaultdict(set)
    )
    for node in xde.nodes:
        first_by_definition.setdefault(node.definition_entry, node)
        if node.color is not None:
            colors_by_definition[node.definition_entry].add(node.color)

    product_ids = {
        key: make_id("product", node.definition_name, key)
        for key, node in first_by_definition.items()
    }
    configuration_ids = {
        key: make_id("configuration", node.definition_name, key)
        for key, node in first_by_definition.items()
    }
    occurrence_ids_by_owner: dict[str, list[str]] = defaultdict(list)
    body_ids_by_product: dict[str, list[str]] = defaultdict(list)
    occurrence_id_by_path: dict[str, str] = {}

    occurrences: list[Occurrence] = []
    roots: list[RootSelection] = []
    stack: list[XdeNode] = []
    for node in xde.nodes:
        stack = stack[: node.depth]
        if node.depth == 0:
            roots.append(
                RootSelection(
                    product_id=product_ids[node.definition_entry],
                    configuration_id=configuration_ids[node.definition_entry],
                )
            )
            occurrence_id_by_path[node.path] = product_ids[node.definition_entry]
            if not _is_identity(node.local_matrix):
                diagnostics.append(
                    ImportDiagnostic(
                        code=f"{prefix}_ROOT_TRANSFORM_NOT_REPRESENTABLE",
                        severity="warning",
                        action="skipped",
                        message=(
                            "A non-identity XDE root placement cannot be represented "
                            "by cad3d-ir RootSelection."
                        ),
                        source_document_id=source.id,
                        source_id=node.label_entry,
                    )
                )
        else:
            if not stack:
                raise StepReadError(
                    f"invalid XDE traversal depth {node.depth} at {node.path}"
                )
            parent = stack[-1]
            occurrence_id = make_id("occurrence", node.occurrence_name, node.path)
            occurrence_id_by_path[node.path] = occurrence_id
            occurrences.append(
                Occurrence(
                    id=occurrence_id,
                    name=node.occurrence_name,
                    product_id=product_ids[node.definition_entry],
                    configuration_id=configuration_ids[node.definition_entry],
                    transform=Transform(matrix=node.local_matrix),
                    visible=node.visible,
                    source=_source_ref(source.id, node, definition=False),
                )
            )
            occurrence_ids_by_owner[parent.definition_entry].append(occurrence_id)
        stack.append(node)

    root_names = [
        first_by_definition[node.definition_entry].definition_name
        for node in xde.nodes
        if node.depth == 0
    ]
    package_name = root_names[0] if len(root_names) == 1 else source.name
    package_slug = slug(package_name)

    package_builder: GlbBuilder | None = None
    package_face_material = 0
    package_edge_material: int | None = None
    materials_by_color: dict[tuple[float, float, float, float], int] = {}
    mesh_by_definition: dict[str, int] = {}
    mesh_bounds_by_definition: dict[str, tuple[Any, Any]] = {}
    mesh_outcome = _MeshOutcome()
    linear_deflection = 0.0
    if mesh_settings is not None:
        root_shapes = [
            XCAFDoc_ShapeTool.GetShape_s(node.definition_label)
            for node in xde.nodes
            if node.depth == 0
        ]
        linear_deflection = max(
            linear_deflection_for(shape, ratio=mesh_settings.linear_deflection_ratio)
            for shape in root_shapes
        )
        package_builder = _new_builder(
            package_name,
            mesh_settings=mesh_settings,
            linear_deflection=linear_deflection,
            prefix=prefix,
        )
        package_face_material = package_builder.add_material(
            "faces", _DEFAULT_FACE_COLOR
        )
        if mesh_settings.include_edges:
            package_edge_material = package_builder.add_material(
                "edges", _EDGE_COLOR, unlit=True
            )

    appearances: list[Appearance] = []
    bodies: list[Body] = []
    representations: list[GeometryRepresentation] = []
    resource_records: list[Any] = []
    for definition_entry, node in first_by_definition.items():
        if node.is_assembly:
            continue

        shape = XCAFDoc_ShapeTool.GetShape_s(node.definition_label)
        if shape.IsNull():
            diagnostics.append(
                ImportDiagnostic(
                    code=f"{prefix}_SHAPE_MISSING",
                    severity="error",
                    action="skipped",
                    message=(
                        "XDE definition has no transferable shape: "
                        f"{node.definition_name}"
                    ),
                    source_document_id=source.id,
                    source_id=definition_entry,
                )
            )
            continue

        product_id = product_ids[definition_entry]
        body_id = make_id("body", node.definition_name, definition_entry)
        source_ref = _source_ref(source.id, node, definition=True)
        shape_bounds = _shape_bounds(shape)
        representation_ids: list[str] = []
        brep_representation_id: str | None = None

        color_values = colors_by_definition[definition_entry]
        appearance_id: str | None = None
        selected_color = _DEFAULT_FACE_COLOR
        if color_values:
            if len(color_values) > 1:
                diagnostics.append(
                    ImportDiagnostic(
                        code=f"{prefix}_OCCURRENCE_APPEARANCE_COLLAPSED",
                        severity="warning",
                        action="approximated",
                        message=(
                            "Multiple occurrence colors were collapsed to one "
                            "definition-level body appearance."
                        ),
                        source_document_id=source.id,
                        source_id=definition_entry,
                        details={"color_count": len(color_values)},
                    )
                )
            selected_color = sorted(color_values)[0]
            appearance_id = make_id(
                "appearance", node.definition_name, definition_entry
            )
            appearances.append(
                Appearance(
                    id=appearance_id,
                    name=f"{node.definition_name} appearance",
                    base_color=selected_color,
                    source=source_ref,
                )
            )

        if options.include_brep:
            brep_representation_id = make_id(
                "representation", node.definition_name, definition_entry
            )
            resource = _write_brep(
                shape=shape,
                resource_id=make_id("resource", node.definition_name, definition_entry),
                resource_uri=f"geometry/{product_id.removeprefix('product-')}.brep",
                resource_name=f"{node.definition_name} OCCT B-Rep",
                source=source_ref,
                resources=resources,
                properties=resource_properties,
            )
            resource_records.append(resource)
            representations.append(
                GeometryRepresentation(
                    id=brep_representation_id,
                    name=f"{node.definition_name} design B-Rep",
                    kind="brep",
                    role="design",
                    fidelity="exact",
                    resource_id=resource.id,
                    units=UnitSystem(length_unit="mm"),
                    bounds=shape_bounds,
                    derivation=RepresentationDerivation(
                        relation="converted",
                        method=f"OCCT {prefix} XDE transfer to BRepTools ASCII v1",
                        source=source_ref,
                        metadata={"occt_version": occt_version()},
                    ),
                    topology_namespace=make_id(
                        "topology", node.definition_name, resource.sha256
                    ),
                    source=source_ref,
                )
            )
            representation_ids.append(brep_representation_id)

        if mesh_settings is not None and package_builder is not None:
            shape_mesh = mesh_shape(
                shape,
                linear_deflection=linear_deflection,
                angular_deflection=mesh_settings.angular_deflection,
                include_edges=mesh_settings.include_edges,
            )
            mesh_outcome.failed_faces += shape_mesh.failed_faces
            mesh_outcome.failed_edges += shape_mesh.failed_edges
            if not shape_mesh.faces and not shape_mesh.edges:
                mesh_outcome.empty_definitions.append(node.definition_name)
            else:
                mesh_outcome.meshed_definitions += 1
                face_material = materials_by_color.get(selected_color)
                if face_material is None:
                    if selected_color == _DEFAULT_FACE_COLOR:
                        face_material = package_face_material
                    else:
                        face_material = package_builder.add_material(
                            f"{node.definition_name} faces", selected_color
                        )
                    materials_by_color[selected_color] = face_material
                mesh_index = package_builder.add_mesh(
                    node.definition_name,
                    extras={"id": body_id, "product": product_id},
                )
                _add_shape_mesh(
                    package_builder,
                    mesh_index,
                    shape_mesh,
                    face_material=face_material,
                    edge_material=package_edge_material,
                )
                mesh_by_definition[definition_entry] = mesh_index
                if shape_mesh.bounds is not None:
                    mesh_bounds_by_definition[definition_entry] = shape_mesh.bounds

                if mesh_settings.body_resources:
                    body_builder = _new_builder(
                        node.definition_name,
                        mesh_settings=mesh_settings,
                        linear_deflection=linear_deflection,
                        prefix=prefix,
                    )
                    body_face = body_builder.add_material("faces", selected_color)
                    body_edge = (
                        body_builder.add_material("edges", _EDGE_COLOR, unlit=True)
                        if mesh_settings.include_edges
                        else None
                    )
                    body_mesh = body_builder.add_mesh(
                        node.definition_name,
                        extras={"id": body_id, "product": product_id},
                    )
                    _add_shape_mesh(
                        body_builder,
                        body_mesh,
                        shape_mesh,
                        face_material=body_face,
                        edge_material=body_edge,
                    )
                    body_builder.add_node(
                        node.definition_name,
                        mesh=body_mesh,
                        extras={"id": product_id, "product": product_id},
                    )
                    payload = body_builder.build()
                    mesh_resource = resources.write_chunks(
                        id=make_id(
                            "resource-mesh", node.definition_name, definition_entry
                        ),
                        name=f"{node.definition_name} visualization mesh",
                        uri=f"geometry/{product_id.removeprefix('product-')}.glb",
                        chunks=_iter_bytes(payload),
                        media_type=GLB_MEDIA_TYPE,
                        encoding=GLB_ENCODING,
                        source=source_ref,
                        properties={"occt_version": occt_version()},
                    )
                    resource_records.append(mesh_resource)
                    mesh_representation_id = make_id(
                        "representation-mesh", node.definition_name, definition_entry
                    )
                    representations.append(
                        GeometryRepresentation(
                            id=mesh_representation_id,
                            name=f"{node.definition_name} visualization mesh",
                            kind="mesh",
                            role="visualization",
                            fidelity="derived",
                            resource_id=mesh_resource.id,
                            units=UnitSystem(length_unit="mm"),
                            bounds=_bbox(shape_mesh.bounds),
                            derivation=RepresentationDerivation(
                                relation="tessellated",
                                source_representation_id=brep_representation_id,
                                method="OCCT BRepMesh_IncrementalMesh to glTF binary",
                                tolerance=linear_deflection,
                                source=source_ref,
                                metadata={
                                    "occt_version": occt_version(),
                                    "angular_deflection_rad": (
                                        mesh_settings.angular_deflection
                                    ),
                                    "triangles": shape_mesh.triangle_count,
                                    "line_segments": shape_mesh.line_segment_count,
                                },
                            ),
                            topology_namespace=make_id(
                                "topology-mesh",
                                node.definition_name,
                                mesh_resource.sha256,
                            ),
                            source=source_ref,
                        )
                    )
                    representation_ids.append(mesh_representation_id)

        bodies.append(
            Body(
                id=body_id,
                name=f"{node.definition_name} body",
                representation_ids=representation_ids,
                appearance_id=appearance_id,
                source=source_ref,
            )
        )
        body_ids_by_product[definition_entry].append(body_id)

    visualizations: list[Visualization] = []
    if mesh_settings is not None and package_builder is not None:
        gltf_node_by_path: dict[str, int] = {}
        world_by_path: dict[str, Matrix4] = {}
        world_bounds: tuple[Any, Any] | None = None
        for node in xde.nodes:
            parent_index: int | None = None
            world = node.local_matrix
            if node.depth > 0:
                parent_path = node.path.rsplit("/", 1)[0] if "/" in node.path else ""
                parent_index = gltf_node_by_path.get(parent_path)
                parent_world = world_by_path.get(parent_path)
                if parent_world is not None:
                    world = _matmul(parent_world, node.local_matrix)
            world_by_path[node.path] = world
            local_bounds = mesh_bounds_by_definition.get(node.definition_entry)
            if local_bounds is not None:
                placed = _transform_bounds(world, local_bounds)
                world_bounds = extend_bounds(world_bounds, [*placed[0], *placed[1]])
            gltf_node_by_path[node.path] = package_builder.add_node(
                node.occurrence_name,
                mesh=mesh_by_definition.get(node.definition_entry),
                parent=parent_index,
                matrix=node.local_matrix,
                extras={
                    "id": occurrence_id_by_path[node.path],
                    "product": product_ids[node.definition_entry],
                },
            )
        payload = package_builder.build()
        preview_resource = resources.write_chunks(
            id=make_id("resource-visualization", package_name, source_digest),
            name=f"{package_name} visualization",
            uri=f"preview/{package_slug}.glb",
            chunks=_iter_bytes(payload),
            media_type=GLB_MEDIA_TYPE,
            encoding=GLB_ENCODING,
            properties={"occt_version": occt_version()},
        )
        resource_records.append(preview_resource)
        visualizations.append(
            Visualization(
                id=make_id("visualization", package_name, source_digest),
                name=f"{package_name} visualization",
                kind="glb",
                scope="package",
                resource_id=preview_resource.id,
                units=UnitSystem(length_unit="mm"),
                bounds=_bbox(world_bounds),
                derivation=RepresentationDerivation(
                    relation="tessellated",
                    method="OCCT BRepMesh_IncrementalMesh to glTF binary",
                    tolerance=linear_deflection,
                    metadata={
                        "occt_version": occt_version(),
                        "angular_deflection_rad": mesh_settings.angular_deflection,
                        "include_edges": mesh_settings.include_edges,
                    },
                ),
                statistics={
                    "triangles": package_builder.triangle_count,
                    "line_segments": package_builder.line_segment_count,
                    "vertices": package_builder.vertex_count,
                    "nodes": package_builder.node_count,
                    "meshes": package_builder.mesh_count,
                },
            )
        )
        if mesh_outcome.failed_faces or mesh_outcome.failed_edges:
            diagnostics.append(
                ImportDiagnostic(
                    code=f"{prefix}_MESH_INCOMPLETE",
                    severity="warning",
                    action="skipped",
                    message=(
                        f"{mesh_outcome.failed_faces} faces and "
                        f"{mesh_outcome.failed_edges} edges produced no triangulation "
                        "and are missing from the visualization."
                    ),
                    source_document_id=source.id,
                    details={
                        "failed_faces": mesh_outcome.failed_faces,
                        "failed_edges": mesh_outcome.failed_edges,
                    },
                )
            )
        if mesh_outcome.empty_definitions:
            diagnostics.append(
                ImportDiagnostic(
                    code=f"{prefix}_MESH_EMPTY_DEFINITIONS",
                    severity="warning",
                    action="skipped",
                    message=(
                        f"{len(mesh_outcome.empty_definitions)} definitions have no "
                        "renderable geometry in the visualization."
                    ),
                    source_document_id=source.id,
                    details={"definitions": mesh_outcome.empty_definitions[:20]},
                )
            )

    product_definitions: list[ProductDefinition] = []
    configurations: list[Configuration] = []
    for definition_entry, node in first_by_definition.items():
        source_ref = _source_ref(source.id, node, definition=True)
        product_definitions.append(
            ProductDefinition(
                id=product_ids[definition_entry],
                name=node.definition_name,
                kind="assembly" if node.is_assembly else "part",
                source_document_id=source.id,
                default_configuration_id=configuration_ids[definition_entry],
                source=source_ref,
            )
        )
        configurations.append(
            Configuration(
                id=configuration_ids[definition_entry],
                name="Default",
                product_id=product_ids[definition_entry],
                body_ids=body_ids_by_product[definition_entry],
                occurrence_ids=occurrence_ids_by_owner[definition_entry],
                source=source_ref,
            )
        )

    diagnostics.append(
        ImportDiagnostic(
            code=f"{prefix}_CONFIGURATIONS_SYNTHESIZED",
            severity="info",
            action="normalized",
            message=(
                f"{prefix}/XDE product definitions were mapped to resolved Default "
                "configurations."
            ),
            source_document_id=source.id,
            details={"configuration_count": len(configurations)},
        )
    )

    return CadPackage(
        format="cad3d-ir",
        version=CURRENT_IR_VERSION,
        id=f"package-{prefix.lower()}-{source_digest[:12]}",
        name=package_name,
        units=UnitSystem(length_unit="mm"),
        coordinate_system=CoordinateSystem(),
        source_documents=[source],
        product_definitions=product_definitions,
        configurations=configurations,
        occurrences=occurrences,
        bodies=bodies,
        representations=representations,
        resources=resource_records,
        appearances=appearances,
        visualizations=visualizations,
        roots=roots,
        metadata={
            "adapter": "cad3d-ir-step",
            "adapter_version": ADAPTER_VERSION,
            "occt_version": occt_version(),
            "source_length_units": list(xde.source_length_units),
            **resource_properties,
        },
    )


def _is_identity(matrix: tuple[tuple[float, ...], ...]) -> bool:
    return all(
        isclose(actual, expected, rel_tol=0.0, abs_tol=1e-12)
        for row, expected_row in zip(matrix[:3], _IDENTITY_3X4, strict=True)
        for actual, expected in zip(row, expected_row, strict=True)
    )


def unsupported_feature_diagnostics(
    *,
    source_id: str,
    counts: dict[str, int],
    prefix: str,
) -> list[ImportDiagnostic]:
    diagnostics: list[ImportDiagnostic] = []
    pmi_keys = (
        "dimensions",
        "geometric_tolerances",
        "datums",
        "legacy_dimensional_tolerances",
    )
    pmi_count = sum(counts[key] for key in pmi_keys)
    if pmi_count:
        diagnostics.append(
            ImportDiagnostic(
                code=f"{prefix}_PMI_NOT_MAPPED",
                severity="warning",
                action="skipped",
                message="PMI/GD&T was read by XDE but is not mapped by cad3d-ir yet.",
                source_document_id=source_id,
                details={key: counts[key] for key in pmi_keys},
            )
        )
    if counts["layers"]:
        diagnostics.append(
            ImportDiagnostic(
                code=f"{prefix}_LAYERS_NOT_MAPPED",
                severity="warning",
                action="skipped",
                message="Presentation layers are not mapped by cad3d-ir yet.",
                source_document_id=source_id,
                details={"layer_count": counts["layers"]},
            )
        )
    if counts["physical_materials"]:
        diagnostics.append(
            ImportDiagnostic(
                code=f"{prefix}_PHYSICAL_MATERIALS_NOT_MAPPED",
                severity="warning",
                action="skipped",
                message="XDE physical materials are not mapped by this adapter yet.",
                source_document_id=source_id,
                details={"material_count": counts["physical_materials"]},
            )
        )
    return diagnostics


def unit_diagnostics(
    *,
    source_id: str,
    source_length_units: tuple[str, ...],
    prefix: str,
) -> list[ImportDiagnostic]:
    mm_names = {"millimetre", "millimeter", "mm"}
    normalized = {unit.casefold() for unit in source_length_units}
    if not normalized or normalized <= mm_names:
        return []
    return [
        ImportDiagnostic(
            code=f"{prefix}_LENGTH_UNITS_NORMALIZED",
            severity="info",
            action="normalized",
            message="OCCT normalized the source geometry to package millimetres.",
            source_document_id=source_id,
            details={"source_length_units": list(source_length_units)},
        )
    ]


def mesh_settings_from(
    options: ImportOptions, *, body_resources: bool
) -> MeshSettings | None:
    """Return tessellation settings when the caller asked for display meshes."""
    if not options.include_visualization_mesh:
        return None
    return MeshSettings(
        linear_deflection_ratio=options.mesh_linear_deflection_ratio,
        angular_deflection=options.mesh_angular_deflection,
        include_edges=options.mesh_include_edges,
        body_resources=body_resources,
    )


def package_statistics(package: CadPackage) -> dict[str, Any]:
    """Counts shared by every XDE-backed importer."""
    statistics: dict[str, Any] = {
        "products": len(package.product_definitions),
        "assemblies": sum(
            product.kind == "assembly" for product in package.product_definitions
        ),
        "parts": sum(product.kind == "part" for product in package.product_definitions),
        "occurrences": len(package.occurrences),
        "bodies": len(package.bodies),
        "resources": len(package.resources),
        "brep_representations": sum(
            representation.kind == "brep" for representation in package.representations
        ),
        "mesh_representations": sum(
            representation.kind == "mesh" for representation in package.representations
        ),
    }
    if package.visualizations:
        visualization = package.visualizations[0]
        statistics["visualization"] = dict(visualization.statistics)
        resource = next(
            item for item in package.resources if item.id == visualization.resource_id
        )
        statistics["visualization"]["byte_length"] = resource.byte_length
    return statistics


__all__ = [
    "ADAPTER_VERSION",
    "MeshSettings",
    "XdeNode",
    "XdeReadResult",
    "hash_file",
    "make_id",
    "map_xde",
    "mesh_settings_from",
    "occt_version",
    "package_statistics",
    "read_xde",
    "slug",
    "unit_diagnostics",
    "unsupported_feature_diagnostics",
]
