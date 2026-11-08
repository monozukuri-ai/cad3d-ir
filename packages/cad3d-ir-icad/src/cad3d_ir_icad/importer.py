"""iCAD SX part/assembly (ICD) importer for cad3d-ir through icadkit.

The adapter reads the final bodies that iCAD SX saves with each part (embedded
Parasolid resources that icadkit converts into exact OCCT solids), places them
by the saved part frames and writes one glTF binary with the same conventions
as the STEP adapter. Native primitives and V7L7 CSG results stand in when a
part stores no saved body. Appearance (palette indices) and the 2D drawing
views of the same file are reported but not mapped.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import icadkit
from cad3d_ir import validate_package
from cad3d_ir.constants import CURRENT_IR_VERSION
from cad3d_ir.diagnostics import ImportDiagnostic, ImportResult
from cad3d_ir.importers import ImportOptions
from cad3d_ir.io.resources import ResourceWriter
from cad3d_ir.mesh import GLB_ENCODING, GLB_MEDIA_TYPE
from cad3d_ir.model import (
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
    SourceDependency,
    SourceDocument,
    SourceRef,
    Transform,
    UnitSystem,
    Visualization,
)

from cad3d_ir_icad.errors import IcadReadError, UnsupportedIRVersionError
from cad3d_ir_icad.shapes import (
    IDENTITY,
    Bounds,
    GlbNode,
    Matrix4,
    PartSolid,
    brep_bytes,
    build_glb,
    diagonal,
    matmul,
    merge_bounds,
    shape_bounds,
    sharing_key,
    transform_bounds,
)

ADAPTER_NAME = "cad3d-ir-icad"
ADAPTER_VERSION = "0.3.1"
SOURCE_FORMAT = "icd"
BREP_MEDIA_TYPE = "application/vnd.opencascade.brep"
BREP_ENCODING = "occt-brep-ascii-v1"

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_SIGNATURE_HEAD = b"MOD0"
_SIGNATURE_TAIL = b"MOD1"
_SIGNATURE_TAIL_AT = 0xFC
_TWO_D_KINDS = frozenset({"2d_global", "2d_view", "registered_part"})
_NO_BODIES = "shape.no_bodies"
_EXTERNAL = "shape.external_reference"
_KERNEL_MISSING = "csg.missing_dependency"
_LEGACY_REASONS = frozenset({"native.legacy_inventory"})


def icadkit_version() -> str:
    try:
        return version("icadkit")
    except PackageNotFoundError:  # pragma: no cover - editable checkouts
        return "unknown"


def slug(value: str) -> str:
    text = _SLUG_RE.sub("-", value.casefold()).strip("-")
    return text[:48] or "part"


def hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    length = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            length += len(chunk)
    return digest.hexdigest(), length


def _iter_bytes(payload: bytes, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
    for start in range(0, len(payload), chunk_size):
        yield payload[start : start + chunk_size]


def version_label(raw_version: bytes) -> str | None:
    """``V8L3`` for the saved ``00 08 00 03`` header bytes, else ``None``."""
    if (
        len(raw_version) != 4
        or raw_version[0] != 0
        or raw_version[2] != 0
        or raw_version[1] == 0
        or raw_version[3] == 0
    ):
        return None
    return f"V{raw_version[1]}L{raw_version[3]}"


def is_icd_signature(head: bytes) -> bool:
    """True for the ``MOD0`` ... ``MOD1`` 256-byte header of an iCAD SX file."""
    return (
        len(head) >= _SIGNATURE_TAIL_AT + 4
        and head[:4] == _SIGNATURE_HEAD
        and head[_SIGNATURE_TAIL_AT : _SIGNATURE_TAIL_AT + 4] == _SIGNATURE_TAIL
    )


@dataclass(frozen=True, slots=True)
class IcadLimits:
    """Bounds handed to icadkit's part-shape reader (a server-sized default)."""

    max_parts: int = 100_000
    max_bodies: int = 50_000
    max_entities: int = 2_000_000
    max_subshapes: int = 500_000
    max_triangles: int = 20_000_000
    max_vertices: int = 40_000_000

    def part_shape_limits(self) -> Any:
        return icadkit.PartShapeLimits(
            max_parts=self.max_parts,
            max_bodies=self.max_bodies,
            saved=icadkit.SavedBodyLimits(
                max_bodies=self.max_bodies,
                max_entities=self.max_entities,
                max_subshapes=self.max_subshapes,
                max_triangles=self.max_triangles,
                max_vertices=self.max_vertices,
            ),
            csg=icadkit.CsgLimits(
                max_bodies=self.max_bodies,
                max_subshapes=self.max_subshapes,
                max_triangles=self.max_triangles,
                max_vertices=self.max_vertices,
            ),
        )


@dataclass(slots=True)
class IcadSource:
    """One ICD file as icadkit reads it, before any mapping to the IR."""

    path: Path
    digest: str
    byte_length: int
    document: Any
    byte_order: str
    raw_version: bytes
    version_label: str | None
    name: str | None
    parts: Any
    part_list: list[Any]
    parts_unqualified: bool
    document_kind: str
    views_unqualified: bool
    view_counts: dict[str, int]
    entities_2d: int
    shapes: Any | None
    shapes_error: str | None
    shapes_seconds: float

    @property
    def has_2d_views(self) -> bool:
        return any(self.view_counts.get(kind, 0) for kind in _TWO_D_KINDS)


def read_source(
    path: Path | str,
    *,
    limits: IcadLimits | None = None,
    read_shapes: bool = True,
) -> IcadSource:
    """Read parts, views and (optionally) part shapes of one ICD file."""
    source_path = Path(path)
    if not source_path.is_file():
        raise IcadReadError(f"ICD source is not a file: {source_path}")
    limits = IcadLimits() if limits is None else limits
    digest, byte_length = hash_file(source_path)
    try:
        document = icadkit.read(source_path)
    except icadkit.IcadError as exc:
        raise IcadReadError(
            f"not a readable iCAD SX document: {source_path}: {exc}"
        ) from exc
    header = document.header
    label = getattr(header, "version_label", None) or version_label(header.raw_version)
    try:
        parts = document.read_parts()
    except icadkit.IcadError as exc:
        raise IcadReadError(f"cannot read the parts of {source_path}: {exc}") from exc
    part_list = list(parts.walk(include_root=True))
    parts_unqualified = any(d.code == "parts.profile" for d in parts.diagnostics)

    views: Any | None
    try:
        views = document.read_views()
    except icadkit.IcadError:
        views = None
    document_kind = "unknown" if views is None else str(views.document_kind)
    views_unqualified = views is None or any(
        d.code == "views.profile" for d in views.diagnostics
    )
    view_counts: dict[str, int] = {}
    if views is not None:
        for view in views.views:
            view_counts[str(view.kind)] = view_counts.get(str(view.kind), 0) + 1
    entities_2d = 0
    if (
        views is not None
        and not views_unqualified
        and any(view_counts.get(kind, 0) for kind in _TWO_D_KINDS)
    ):
        try:
            entities_2d = len(document.read_drawing().entities)
        except icadkit.IcadError:
            entities_2d = 0

    shapes: Any | None = None
    shapes_error: str | None = None
    started = time.perf_counter()
    if read_shapes and part_list and not parts_unqualified:
        try:
            shapes = icadkit.read_part_shapes(
                document, frame="part", limits=limits.part_shape_limits()
            )
        except icadkit.IcadError as exc:
            shapes_error = f"{type(exc).__name__}: {exc}"
    return IcadSource(
        path=source_path,
        digest=digest,
        byte_length=byte_length,
        document=document,
        byte_order=str(header.byte_order),
        raw_version=bytes(header.raw_version),
        version_label=label,
        name=header.name_cp932_candidate,
        parts=parts,
        part_list=part_list,
        parts_unqualified=parts_unqualified,
        document_kind=document_kind,
        views_unqualified=views_unqualified,
        view_counts=view_counts,
        entities_2d=entities_2d,
        shapes=shapes,
        shapes_error=shapes_error,
        shapes_seconds=time.perf_counter() - started,
    )


# -- classification ----------------------------------------------------------------


@dataclass(slots=True)
class PartClassification:
    """What each part record contributes: a solid, an external reference, or nothing."""

    solids: dict[str, PartSolid] = field(default_factory=dict)
    external: dict[str, str | None] = field(default_factory=dict)
    with_bodies: int = 0
    partial: int = 0
    unsupported_reasons: Counter[str] = field(default_factory=Counter)
    body_sources: Counter[str] = field(default_factory=Counter)
    kernel_missing: bool = False
    legacy_geometry: int = 0


def classify(source: IcadSource) -> PartClassification:
    """Pair every part record with its converted shape, if any."""
    result = PartClassification()
    shapes_by_part: dict[str, Any] = {}
    if source.shapes is not None:
        shapes_by_part = {shape.part_id: shape for shape in source.shapes.shapes}
    for part in source.part_list:
        shape = shapes_by_part.get(part.part_id)
        reference = getattr(part, "external_reference", None)
        if bool(getattr(part, "is_external", False)) or (
            shape is not None and shape.reason == _EXTERNAL
        ):
            result.external[part.part_id] = (
                None if reference is None else getattr(reference, "name", None)
            )
            continue
        if shape is None:
            continue
        if str(shape.status) == "unsupported" and shape.reason == _NO_BODIES:
            continue
        result.with_bodies += 1
        if str(shape.status) == "partial":
            result.partial += 1
        if shape.shape is not None:
            bounds = shape_bounds(shape.shape)
            world = part.placement.world_transform if part.placement else None
            result.solids[part.part_id] = PartSolid(
                part_id=part.part_id,
                name=part.name or "",
                shape=shape.shape,
                key=sharing_key(
                    shape.bodies,
                    bounds,
                    mirrored=bool(getattr(part, "is_mirror", False)),
                ),
                sources=tuple(
                    str(body.source)
                    for body in shape.bodies
                    if body.status == "complete"
                ),
                bounds=bounds,
                world_transform=world if world is not None else IDENTITY,
            )
            for body in shape.bodies:
                if body.status == "complete":
                    result.body_sources[str(body.source)] += 1
        reason = shape.reason
        if shape.shape is None or str(shape.status) == "partial":
            codes = [reason] if reason else []
            codes += [
                code
                for body in shape.bodies
                if body.status != "complete"
                for code in (body.diagnostics[0].code if body.diagnostics else None,)
                if code
            ]
            for code in codes or ["unknown"]:
                result.unsupported_reasons[code] += 1
                if code == _KERNEL_MISSING:
                    result.kernel_missing = True
                if code in _LEGACY_REASONS:
                    result.legacy_geometry += 1
    return result


# -- mapping -------------------------------------------------------------------------


def _rigid_inverse(matrix: Matrix4) -> Matrix4:
    rows = [tuple(float(v) for v in row) for row in matrix[:3]]
    translation = tuple(row[3] for row in rows)
    inverse = []
    for column in range(3):
        rotation = tuple(rows[k][column] for k in range(3))
        shift = -sum(rotation[k] * translation[k] for k in range(3))
        inverse.append((*rotation, shift))
    inverse.append((0.0, 0.0, 0.0, 1.0))
    return tuple(inverse)


def _as_matrix(matrix: Matrix4 | None) -> Matrix4 | None:
    if matrix is None:
        return None
    rows = tuple(tuple(float(v) for v in row) for row in matrix)
    if len(rows) != 4 or any(len(row) != 4 for row in rows):
        return None
    return rows


@dataclass(slots=True)
class _Node:
    index: int
    part: Any
    depth: int
    parent: _Node | None
    matrix: Matrix4
    world: Matrix4 | None
    placement_unqualified: bool
    product_id: str = ""
    configuration_id: str = ""
    occurrence_id: str | None = None
    body_id: str | None = None
    children: list[_Node] = field(default_factory=list)


def _part_tree(source: IcadSource) -> tuple[list[_Node], int]:
    """Depth-first part nodes with local transforms; orphans become extra roots."""
    by_id = {part.part_id: part for part in source.part_list}
    children: dict[str, list[Any]] = {}
    roots: list[Any] = []
    for part in source.part_list:
        parent_id = getattr(part, "parent_id", None)
        if (
            bool(getattr(part, "is_root", False))
            or parent_id is None
            or parent_id not in by_id
        ):
            roots.append(part)
        else:
            children.setdefault(parent_id, []).append(part)
    nodes: list[_Node] = []
    visited: set[str] = set()

    def visit(part: Any, parent: _Node | None, depth: int) -> None:
        if part.part_id in visited:
            return
        visited.add(part.part_id)
        placement = getattr(part, "placement", None)
        world = _as_matrix(getattr(placement, "world_transform", None))
        local = _as_matrix(getattr(placement, "local_transform", None))
        unqualified = False
        if parent is None:
            matrix = world if world is not None else IDENTITY
        elif local is not None:
            matrix = local
        elif world is not None and parent.world is not None:
            matrix = matmul(_rigid_inverse(parent.world), world)
        else:
            matrix = IDENTITY
            unqualified = True
        node = _Node(len(nodes), part, depth, parent, matrix, world, unqualified)
        nodes.append(node)
        if parent is not None:
            parent.children.append(node)
        for child in children.get(part.part_id, []):
            visit(child, node, depth + 1)

    for root in roots:
        visit(root, None, 0)
    orphans = 0
    for part in source.part_list:
        if part.part_id not in visited:
            orphans += 1
            visit(part, None, 0)
    return nodes, orphans


def _diagnostic(
    code: str,
    severity: str,
    action: str,
    message: str,
    source_id: str,
    details: dict[str, Any] | None = None,
) -> ImportDiagnostic:
    return ImportDiagnostic(
        code=code,
        severity=severity,  # type: ignore[arg-type]
        action=action,  # type: ignore[arg-type]
        message=message,
        source_document_id=source_id,
        details=details,
    )


class IcadImporter:
    """Importer for iCAD SX models (``.icd``), parts and assemblies alike.

    ``limits`` bounds icadkit's part-shape reader; ``read_views`` controls
    whether the 2D drawing views of the file are counted (they are never
    imported here). The adapter needs icadkit's ``preview`` extra
    (``parasolid-kit[occt]``), which the distribution requires.
    """

    format_name = SOURCE_FORMAT
    suffixes = frozenset({".icd"})

    def __init__(self, *, limits: IcadLimits | None = None) -> None:
        self.limits = IcadLimits() if limits is None else limits

    @property
    def accepted_suffixes(self) -> frozenset[str]:
        return self.suffixes

    def probe(self, path: Path) -> float:
        """Return a bounded confidence from the ``MOD0`` / ``MOD1`` header signature."""
        source_path = Path(path)
        try:
            if not source_path.is_file():
                return 0.0
            with source_path.open("rb") as stream:
                head = stream.read(_SIGNATURE_TAIL_AT + 4)
        except OSError:
            return 0.0
        if not is_icd_signature(head):
            return 0.0
        return 0.9 if source_path.suffix.lower() in self.suffixes else 0.6

    def import_file(
        self,
        path: Path,
        *,
        options: ImportOptions,
        resources: ResourceWriter,
    ) -> ImportResult:
        """Map one ICD file to a package with one product per part record."""
        if options.ir_version != CURRENT_IR_VERSION:
            raise UnsupportedIRVersionError(
                f"{ADAPTER_NAME} supports IR {CURRENT_IR_VERSION}, "
                f"not {options.ir_version}"
            )
        source = read_source(path, limits=self.limits)
        return map_source(source, options=options, resources=resources)


def map_source(
    source: IcadSource,
    *,
    options: ImportOptions,
    resources: ResourceWriter,
) -> ImportResult:
    """Map an already read :class:`IcadSource` to an :class:`ImportResult`."""
    key = source.digest[:12]
    stem = source.path.stem or "model"
    name_slug = slug(stem)
    source_id = f"source-{SOURCE_FORMAT}-{key}"
    label = source.version_label
    source_ref = SourceRef(
        document_id=source_id,
        source_kind="icadkit-document",
        source_id=label,
    )
    diagnostics: list[ImportDiagnostic] = []
    classification = classify(source)
    nodes, orphans = _part_tree(source)

    # Version and reader state -----------------------------------------------
    unqualified = source.parts_unqualified and source.views_unqualified
    if unqualified:
        diagnostics.append(
            _diagnostic(
                "ICD_VERSION_UNQUALIFIED",
                "error",
                "unresolved",
                (
                    f"iCAD SX version {label or 'of unknown layout'} "
                    f"({source.byte_order}-endian) has not been qualified by "
                    "icadkit; the parts, geometry and 2D views were not read."
                ),
                source_id,
                {"version_label": label, "byte_order": source.byte_order},
            )
        )
    elif source.parts_unqualified or (
        classification.with_bodies == 0
        and classification.legacy_geometry == 0
        and not source.part_list
        and not source.views_unqualified
        and not source.has_2d_views
    ):
        diagnostics.append(
            _diagnostic(
                "ICD_LEGACY_VERSION_NO_GEOMETRY",
                "warning",
                "skipped",
                (
                    f"iCAD SX version {label or 'of unknown layout'} "
                    f"({source.byte_order}-endian) stores parts in a layout icadkit "
                    "reads for views only; no 3D geometry is available from it."
                ),
                source_id,
                {"version_label": label, "byte_order": source.byte_order},
            )
        )
    if source.shapes_error is not None:
        diagnostics.append(
            _diagnostic(
                "ICD_SHAPE_LIMIT",
                "error",
                "unresolved",
                f"icadkit stopped converting the part shapes: {source.shapes_error}",
                source_id,
                {"error": source.shapes_error},
            )
        )
    if classification.kernel_missing:
        diagnostics.append(
            _diagnostic(
                "ICD_KERNEL_MISSING",
                "error",
                "unresolved",
                (
                    "The geometry kernel is not installed (icadkit's preview extra, "
                    "parasolid-kit[occt]); saved bodies cannot be converted."
                ),
                source_id,
            )
        )
    if classification.legacy_geometry:
        diagnostics.append(
            _diagnostic(
                "ICD_LEGACY_VERSION_NO_GEOMETRY",
                "warning",
                "skipped",
                (
                    f"{classification.legacy_geometry} parts keep their native "
                    "geometry in a V7L2-V7L5 layout that icadkit does not evaluate; "
                    "only their saved final bodies, if any, are shown."
                ),
                source_id,
                {"parts": classification.legacy_geometry},
            )
        )

    # Geometry -------------------------------------------------------------------
    solids = classification.solids
    world_bounds: Bounds | None = None
    for solid in solids.values():
        if solid.bounds is not None:
            world_bounds = merge_bounds(
                world_bounds, transform_bounds(solid.bounds, solid.world_transform)
            )
    linear_deflection = max(
        diagonal(world_bounds) * options.mesh_linear_deflection_ratio, 1e-6
    )
    shapes_by_key: dict[str, Any] = {}
    for solid in solids.values():
        shapes_by_key.setdefault(solid.key, solid.shape)

    # Identifiers ------------------------------------------------------------------
    for node in nodes:
        part_slug = slug(node.part.name or "")
        suffix = f"{key}-{node.index}"
        node.product_id = f"product-{part_slug}-{suffix}"
        node.configuration_id = f"configuration-{part_slug}-{suffix}"
        if node.parent is not None:
            node.occurrence_id = f"occurrence-{part_slug}-{suffix}"
        if node.part.part_id in solids:
            node.body_id = f"body-{part_slug}-{suffix}"

    glb_nodes = [
        GlbNode(
            name=node.part.name or f"part {node.index}",
            parent=None if node.parent is None else node.parent.index,
            matrix=node.matrix,
            key=solids[node.part.part_id].key if node.part.part_id in solids else None,
            extras={
                "id": node.occurrence_id or node.product_id,
                "product": node.product_id,
                "part": node.part.part_id,
            },
        )
        for node in nodes
    ]

    resource_records: list[Any] = []
    representations: list[GeometryRepresentation] = []
    visualizations: list[Visualization] = []
    brep_representation_by_key: dict[str, str] = {}
    glb_result = None
    if solids and options.include_visualization_mesh:
        glb_result = build_glb(
            glb_nodes,
            shapes_by_key,
            linear_deflection=linear_deflection,
            angular_deflection=options.mesh_angular_deflection,
            include_edges=options.mesh_include_edges,
            generator=f"{ADAPTER_NAME} {ADAPTER_VERSION}",
            root_name=stem,
            root_extras={
                "cad3d_ir_icad": {
                    "source_format": SOURCE_FORMAT,
                    "icadkit_version": icadkit_version(),
                    "version_label": label,
                    "linear_deflection_mm": linear_deflection,
                    "angular_deflection_rad": options.mesh_angular_deflection,
                }
            },
        )
        if glb_result.failed_keys:
            diagnostics.append(
                _diagnostic(
                    "ICD_MESH_FAILED",
                    "warning",
                    "skipped",
                    (
                        f"{len(glb_result.failed_keys)} converted solids could not be "
                        "tessellated and are left out of the visualization."
                    ),
                    source_id,
                    {"shapes": dict(list(glb_result.failed_keys.items())[:20])},
                )
            )
        resource = resources.write_chunks(
            id=f"resource-{name_slug}-{key}",
            name=f"{stem} visualization mesh",
            uri=f"geometry/{name_slug}-{key}.glb",
            chunks=_iter_bytes(glb_result.payload),
            media_type=GLB_MEDIA_TYPE,
            encoding=GLB_ENCODING,
            source=source_ref,
            properties={"icadkit_version": icadkit_version()},
        )
        resource_records.append(resource)
        placed = glb_result.placed_bounds or world_bounds
        bounds = None if placed is None else BBox3(min=placed[0], max=placed[1])
        visualizations.append(
            Visualization(
                id=f"visualization-{name_slug}-{key}",
                name=f"{stem} visualization",
                kind="glb",
                scope="package",
                resource_id=resource.id,
                units=UnitSystem(length_unit="mm"),
                bounds=bounds,
                derivation=RepresentationDerivation(
                    relation="tessellated",
                    method=(
                        "icadkit part shapes (saved final bodies, CSG results, native "
                        "primitives) through OCCT BRepMesh_IncrementalMesh to "
                        "glTF binary"
                    ),
                    tolerance=linear_deflection,
                    source=source_ref,
                    metadata={
                        "include_edges": options.mesh_include_edges,
                        "angular_deflection_rad": options.mesh_angular_deflection,
                        "shared_meshes": len(shapes_by_key),
                    },
                ),
                statistics={
                    "triangles": glb_result.triangle_count,
                    "line_segments": glb_result.line_segment_count,
                    "placed_triangles": glb_result.placed_triangles,
                    "placed_line_segments": glb_result.placed_line_segments,
                    "vertices": glb_result.vertex_count,
                    "nodes": glb_result.node_count,
                    "meshes": glb_result.mesh_count,
                    "parts": len(solids),
                },
                source=source_ref,
            )
        )
    if solids and options.include_brep:
        for index, (shape_key, shape) in enumerate(shapes_by_key.items()):
            payload = brep_bytes(shape)
            brep_resource = resources.write_chunks(
                id=f"resource-brep-{shape_key}-{key}",
                name=f"{stem} B-Rep {index}",
                uri=f"geometry/{name_slug}-{key}-{index}.brep",
                chunks=_iter_bytes(payload),
                media_type=BREP_MEDIA_TYPE,
                encoding=BREP_ENCODING,
                source=source_ref,
                properties={"icadkit_version": icadkit_version(), "frame": "part"},
            )
            resource_records.append(brep_resource)
            representation_id = f"representation-brep-{shape_key}-{key}"
            owner = next(s for s in solids.values() if s.key == shape_key)
            representations.append(
                GeometryRepresentation(
                    id=representation_id,
                    name=f"{owner.name or stem} B-Rep",
                    kind="brep",
                    role="design",
                    fidelity="exact",
                    resource_id=brep_resource.id,
                    units=UnitSystem(length_unit="mm"),
                    bounds=(
                        None
                        if owner.bounds is None
                        else BBox3(min=owner.bounds[0], max=owner.bounds[1])
                    ),
                    derivation=RepresentationDerivation(
                        relation="converted",
                        method="icadkit saved-body conversion to OCCT BRepTools ASCII",
                        source=source_ref,
                        metadata={"sources": list(owner.sources)},
                    ),
                    source=source_ref,
                )
            )
            brep_representation_by_key[shape_key] = representation_id

    # Package objects ---------------------------------------------------------------
    product_definitions: list[ProductDefinition] = []
    configurations: list[Configuration] = []
    occurrences: list[Occurrence] = []
    bodies: list[Body] = []
    dependencies: list[SourceDependency] = []
    seen_references: set[str] = set()
    for node in nodes:
        part = node.part
        part_ref = SourceRef(
            document_id=source_id,
            source_kind="icadkit-part",
            source_id=part.part_id,
            metadata={"source_id": int(getattr(part, "source_id", 0) or 0)},
        )
        properties: dict[str, Any] = {}
        comment = getattr(part, "comment", None)
        if comment:
            properties["comment"] = comment
        if part.part_id in classification.external:
            reference = classification.external[part.part_id]
            properties["external_reference"] = reference
            if reference and reference not in seen_references:
                seen_references.add(reference)
                dependencies.append(
                    SourceDependency(
                        id=f"dependency-{slug(reference)}-{key}-{len(dependencies)}",
                        name=reference,
                        uri=reference,
                        kind="component",
                        required=True,
                    )
                )
        if bool(getattr(part, "is_mirror", False)):
            properties["mirrored"] = True
        product_definitions.append(
            ProductDefinition(
                id=node.product_id,
                name=part.name or None,
                kind="assembly" if node.children else "part",
                source_document_id=source_id,
                default_configuration_id=node.configuration_id,
                source=part_ref,
                properties=properties,
            )
        )
        if node.body_id is not None:
            solid = solids[part.part_id]
            representation_ids = (
                [brep_representation_by_key[solid.key]]
                if solid.key in brep_representation_by_key
                else []
            )
            bodies.append(
                Body(
                    id=node.body_id,
                    name=f"{part.name or 'part'} body",
                    representation_ids=representation_ids,
                    source=part_ref,
                    properties={"sources": list(solid.sources)},
                )
            )
        configurations.append(
            Configuration(
                id=node.configuration_id,
                name="Default",
                product_id=node.product_id,
                body_ids=[node.body_id] if node.body_id is not None else [],
                occurrence_ids=[
                    child.occurrence_id
                    for child in node.children
                    if child.occurrence_id is not None
                ],
                source=part_ref,
            )
        )
        if node.occurrence_id is not None:
            occurrences.append(
                Occurrence(
                    id=node.occurrence_id,
                    name=part.name or None,
                    product_id=node.product_id,
                    configuration_id=node.configuration_id,
                    transform=Transform(matrix=node.matrix),  # type: ignore[arg-type]
                    source=part_ref,
                )
            )

    # Remaining diagnostics ---------------------------------------------------------
    if solids:
        diagnostics.append(
            _diagnostic(
                "ICD_SAVED_BREP",
                "info",
                "preserved",
                (
                    "Geometry comes from the solids iCAD SX saved with each part "
                    "(embedded Parasolid bodies converted by icadkit), placed by the "
                    "saved part frames; exact surfaces are kept."
                ),
                source_id,
                {
                    "parts_converted": len(solids),
                    "bodies": dict(classification.body_sources),
                    "shared_meshes": len(shapes_by_key),
                },
            )
        )
        diagnostics.append(
            _diagnostic(
                "ICD_APPEARANCE_NOT_MAPPED",
                "info",
                "skipped",
                (
                    "iCAD SX stores colours as palette indices that icadkit does not "
                    "map to RGB; the default face colour is used."
                ),
                source_id,
            )
        )
    failed_parts = classification.with_bodies - len(solids)
    if failed_parts > 0 and not classification.kernel_missing:
        diagnostics.append(
            _diagnostic(
                "ICD_BODY_UNSUPPORTED",
                "warning",
                "skipped",
                (
                    f"{failed_parts} of {classification.with_bodies} parts with saved "
                    "bodies could not be converted and are left out."
                ),
                source_id,
                {
                    "parts": failed_parts,
                    "reasons": dict(classification.unsupported_reasons.most_common(12)),
                },
            )
        )
    elif classification.partial:
        diagnostics.append(
            _diagnostic(
                "ICD_BODY_UNSUPPORTED",
                "warning",
                "skipped",
                (
                    f"{classification.partial} parts converted only some of their "
                    "saved bodies."
                ),
                source_id,
                {
                    "parts": classification.partial,
                    "reasons": dict(classification.unsupported_reasons.most_common(12)),
                },
            )
        )
    if classification.external:
        names = sorted({n for n in classification.external.values() if n})
        diagnostics.append(
            _diagnostic(
                "ICD_EXTERNAL_PART_MISSING",
                "warning",
                "unresolved",
                (
                    f"{len(classification.external)} parts are external references "
                    "whose geometry lives in other .icd files that were not provided."
                ),
                source_id,
                {"parts": len(classification.external), "references": names[:50]},
            )
        )
    unqualified_placements = sum(
        1
        for node in nodes
        if node.placement_unqualified and node.part.part_id in solids
    )
    if unqualified_placements:
        diagnostics.append(
            _diagnostic(
                "ICD_PLACEMENT_UNQUALIFIED",
                "warning",
                "approximated",
                (
                    f"{unqualified_placements} parts have no qualified frame; they are "
                    "placed at the origin of their parent."
                ),
                source_id,
                {"parts": unqualified_placements},
            )
        )
    if orphans:
        diagnostics.append(
            _diagnostic(
                "ICD_PART_ORPHAN",
                "warning",
                "approximated",
                f"{orphans} part records are not linked to the root and are "
                "placed as roots.",
                source_id,
                {"parts": orphans},
            )
        )
    if source.document_kind == "registered_part":
        diagnostics.append(
            _diagnostic(
                "ICD_REGISTERED_PART",
                "warning",
                "skipped",
                "The file is a registered 2D part (a library symbol); it holds "
                "no 3D model.",
                source_id,
            )
        )
    if source.has_2d_views:
        diagnostics.append(
            _diagnostic(
                "ICD_2D_VIEWS_PRESENT",
                "info",
                "skipped",
                (
                    "The file also stores 2D drawing views; they are not part of the "
                    "3D package."
                ),
                source_id,
                {"views": dict(source.view_counts), "entities": source.entities_2d},
            )
        )
    if not solids and not unqualified:
        severity = (
            "warning" if (source.has_2d_views or classification.external) else "error"
        )
        diagnostics.append(
            _diagnostic(
                "ICD_NO_GEOMETRY",
                severity,
                "unresolved",
                "No part of the file yielded a displayable solid.",
                source_id,
                {
                    "parts": len(source.part_list),
                    "external_parts": len(classification.external),
                    "views_2d": sum(source.view_counts.get(k, 0) for k in _TWO_D_KINDS),
                },
            )
        )
    if options.strict and not solids:
        reason = (
            "unqualified version"
            if unqualified
            else "no displayable solid"
            if not classification.external
            else "only external part references"
        )
        raise IcadReadError(f"{reason} in iCAD SX document: {source.path}")

    # Package --------------------------------------------------------------------
    source_document = SourceDocument(
        id=source_id,
        name=source.path.name,
        format=SOURCE_FORMAT,
        version=None if label is None else f"iCAD SX {label}",
        uri=source.path.name,
        sha256=source.digest,
        dependencies=dependencies,
        properties={
            "byte_order": source.byte_order,
            "raw_version_hex": source.raw_version.hex(),
            "version_label": label,
            "document_kind": source.document_kind,
            "saved_name": source.name,
            "view_counts": dict(source.view_counts),
        },
    )
    roots = [
        RootSelection(
            product_id=node.product_id, configuration_id=node.configuration_id
        )
        for node in nodes
        if node.parent is None
    ]
    metadata: dict[str, Any] = {
        "adapter": ADAPTER_NAME,
        "adapter_version": ADAPTER_VERSION,
        "icadkit_version": icadkit_version(),
        "source_format": SOURCE_FORMAT,
        "version_label": label,
        "byte_order": source.byte_order,
        "document_kind": source.document_kind,
    }
    package = CadPackage(
        format="cad3d-ir",
        version=CURRENT_IR_VERSION,
        id=f"package-{SOURCE_FORMAT}-{key}",
        name=stem,
        units=UnitSystem(length_unit="mm"),
        coordinate_system=CoordinateSystem(),
        source_documents=[source_document],
        product_definitions=product_definitions,
        configurations=configurations,
        occurrences=occurrences,
        bodies=bodies,
        representations=representations,
        resources=resource_records,
        visualizations=visualizations,
        roots=roots,
        metadata=metadata,
    )
    if options.validate:
        validate_package(package)

    statistics: dict[str, Any] = {
        "source_byte_length": source.byte_length,
        "source_format": SOURCE_FORMAT,
        "icd_version": label,
        "byte_order": source.byte_order,
        "document_kind": source.document_kind,
        "version_unqualified": unqualified,
        "parts": len(source.part_list),
        "parts_with_bodies": classification.with_bodies,
        "parts_converted": len(solids),
        "parts_partial": classification.partial,
        "parts_unsupported": max(classification.with_bodies - len(solids), 0),
        "external_parts": len(classification.external),
        "orphan_parts": orphans,
        "bodies": sum(classification.body_sources.values()),
        "bodies_by_source": dict(classification.body_sources),
        "unsupported_reasons": dict(classification.unsupported_reasons),
        "unique_meshes": len(shapes_by_key),
        "products": len(product_definitions),
        "occurrences": len(occurrences),
        "triangles": 0 if glb_result is None else glb_result.triangle_count,
        "line_segments": 0 if glb_result is None else glb_result.line_segment_count,
        "placed_triangles": 0 if glb_result is None else glb_result.placed_triangles,
        "vertices": 0 if glb_result is None else glb_result.vertex_count,
        "nodes": 0 if glb_result is None else glb_result.node_count,
        "visualization_byte_length": 0
        if glb_result is None
        else len(glb_result.payload),
        "linear_deflection_mm": linear_deflection,
        "views_2d": sum(source.view_counts.get(k, 0) for k in _TWO_D_KINDS),
        "entities_2d": source.entities_2d,
        "shapes_seconds": round(source.shapes_seconds, 3),
    }
    return ImportResult(package=package, diagnostics=diagnostics, statistics=statistics)


__all__ = [
    "ADAPTER_NAME",
    "ADAPTER_VERSION",
    "BREP_ENCODING",
    "BREP_MEDIA_TYPE",
    "IcadImporter",
    "IcadLimits",
    "IcadSource",
    "PartClassification",
    "classify",
    "icadkit_version",
    "is_icd_signature",
    "map_source",
    "read_source",
    "version_label",
]
