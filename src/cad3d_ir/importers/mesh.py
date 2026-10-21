"""Built-in importer for mesh-only sources (STL and Wavefront OBJ).

These formats carry no product structure, units, or exact geometry, so the
mapping is deliberately small: one source document, one part with a resolved
``Default`` configuration, one body, one ``mesh`` representation whose
resource is the glTF binary written by :class:`cad3d_ir.mesh.GlbBuilder`, and
one package-level visualization that points at the same resource.

Units stay ``unknown`` in the package; the GLB root assumes millimetres for
display and says so in a diagnostic. Triangles are written de-indexed with one
flat normal per face, which reproduces the facet shading these formats imply.
"""

from __future__ import annotations

import re
import struct
from collections.abc import Iterator
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal

from cad3d_ir.constants import CURRENT_IR_VERSION
from cad3d_ir.diagnostics import ImportDiagnostic, ImportResult
from cad3d_ir.importers.base import ImportOptions
from cad3d_ir.io.resources import ResourceWriter
from cad3d_ir.mesh.geometry import drop_degenerate_triangles, flat_normals
from cad3d_ir.mesh.glb import GLB_ENCODING, GLB_MEDIA_TYPE, GlbBuilder
from cad3d_ir.model import (
    BBox3,
    Body,
    CadPackage,
    Configuration,
    CoordinateSystem,
    GeometryRepresentation,
    ProductDefinition,
    RepresentationDerivation,
    RootSelection,
    SourceDocument,
    SourceRef,
    UnitSystem,
    Visualization,
)
from cad3d_ir.validation import validate_package

MeshFormat = Literal["stl-ascii", "stl-binary", "obj"]

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_STL_HEADER = 80
_STL_RECORD = 50
_ASCII_PROBE = 4096
_FACE_COLOR = (0.55, 0.62, 0.70, 1.0)
_LINE_COLOR = (0.08, 0.10, 0.14, 1.0)


class MeshImportError(ValueError):
    """Raised when a mesh source cannot be read into a package."""


@dataclass(slots=True)
class MeshData:
    """A de-indexed triangle soup plus optional polylines, in source units."""

    triangles: list[float] = field(default_factory=list)
    polylines: list[list[float]] = field(default_factory=list)
    degenerate_triangles: int = 0
    polygons_triangulated: int = 0
    materials_present: bool = False
    groups_present: bool = False
    binary_length_mismatch: bool = False

    @property
    def triangle_count(self) -> int:
        return len(self.triangles) // 9


def detect_mesh_format(path: Path) -> MeshFormat | None:
    """Sniff STL (ASCII or binary) and OBJ without reading the whole file."""
    try:
        size = path.stat().st_size
        with path.open("rb") as stream:
            head = stream.read(_ASCII_PROBE)
    except OSError:
        return None
    if not head:
        return None
    suffix = path.suffix.lower()
    stripped = head.lstrip()
    if suffix == ".stl" or stripped[:5].lower() == b"solid" or size >= 84:
        if _looks_like_binary_stl(head, size):
            return "stl-binary"
        if stripped[:5].lower() == b"solid" and b"facet" in head.lower():
            return "stl-ascii"
        if suffix == ".stl":
            return "stl-ascii" if stripped[:5].lower() == b"solid" else "stl-binary"
    if suffix == ".obj" or _looks_like_obj(head):
        return "obj"
    return None


def _looks_like_binary_stl(head: bytes, size: int) -> bool:
    if size < _STL_HEADER + 4:
        return False
    count = struct.unpack_from("<I", head, _STL_HEADER)[0]
    if count == 0:
        return size == _STL_HEADER + 4
    return size == _STL_HEADER + 4 + count * _STL_RECORD


def _looks_like_obj(head: bytes) -> bool:
    try:
        text = head.decode("utf-8", errors="replace")
    except UnicodeDecodeError:  # pragma: no cover - replace never raises
        return False
    for line in text.splitlines():
        token = line.strip().split(None, 1)
        if token and token[0] in {"v", "f", "vn", "vt", "o", "g", "mtllib"}:
            return True
    return False


def read_stl(path: Path) -> MeshData:
    """Read an ASCII or binary STL into a de-indexed triangle soup."""
    detected = detect_mesh_format(path)
    if detected == "stl-binary":
        return _read_binary_stl(path)
    if detected == "stl-ascii":
        return _read_ascii_stl(path)
    raise MeshImportError(f"not an STL file: {path}")


def _read_binary_stl(path: Path) -> MeshData:
    data = MeshData()
    with path.open("rb") as stream:
        stream.read(_STL_HEADER)
        count_bytes = stream.read(4)
        if len(count_bytes) < 4:
            raise MeshImportError(f"binary STL is truncated: {path}")
        declared = struct.unpack("<I", count_bytes)[0]
        payload = stream.read()
    available = len(payload) // _STL_RECORD
    if available != declared or len(payload) % _STL_RECORD:
        data.binary_length_mismatch = True
    triangles = data.triangles
    for record in struct.iter_unpack("<12fH", payload[: available * _STL_RECORD]):
        triangles.extend(record[3:12])
    return data


def _read_ascii_stl(path: Path) -> MeshData:
    data = MeshData()
    triangles = data.triangles
    pending: list[float] = []
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        for line in stream:
            parts = line.split()
            if len(parts) == 4 and parts[0] == "vertex":
                try:
                    pending.extend((float(parts[1]), float(parts[2]), float(parts[3])))
                except ValueError as error:
                    raise MeshImportError(f"invalid STL vertex in {path}") from error
            elif parts and parts[0] == "endfacet":
                if len(pending) == 9:
                    triangles.extend(pending)
                pending = []
    return data


def read_obj(path: Path) -> MeshData:
    """Read Wavefront OBJ faces (any polygon size) and ``l`` polylines."""
    data = MeshData()
    vertices: list[tuple[float, float, float]] = []
    triangles = data.triangles
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        for raw_line in stream:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            keyword = parts[0]
            if keyword == "v" and len(parts) >= 4:
                try:
                    vertices.append((float(parts[1]), float(parts[2]), float(parts[3])))
                except ValueError as error:
                    raise MeshImportError(f"invalid OBJ vertex in {path}") from error
            elif keyword == "f":
                corners = [
                    _obj_index(token, len(vertices), path) for token in parts[1:]
                ]
                if len(corners) < 3:
                    continue
                if len(corners) > 3:
                    data.polygons_triangulated += 1
                anchor = vertices[corners[0]]
                for first, second in zip(corners[1:-1], corners[2:], strict=False):
                    triangles.extend(anchor)
                    triangles.extend(vertices[first])
                    triangles.extend(vertices[second])
            elif keyword == "l":
                points = [_obj_index(token, len(vertices), path) for token in parts[1:]]
                if len(points) >= 2:
                    polyline: list[float] = []
                    for index in points:
                        polyline.extend(vertices[index])
                    data.polylines.append(polyline)
            elif keyword in {"mtllib", "usemtl"}:
                data.materials_present = True
            elif keyword in {"g", "o"}:
                data.groups_present = True
    return data


def _obj_index(token: str, vertex_count: int, path: Path) -> int:
    try:
        value = int(token.split("/", 1)[0])
    except ValueError as error:
        raise MeshImportError(f"invalid OBJ face index {token!r} in {path}") from error
    index = value - 1 if value > 0 else vertex_count + value
    if not 0 <= index < vertex_count:
        raise MeshImportError(f"OBJ face index {token!r} is out of range in {path}")
    return index


def _hash_file(path: Path) -> tuple[str, int]:
    digest = sha256()
    byte_length = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            byte_length += len(chunk)
    return digest.hexdigest(), byte_length


def _slug(value: str) -> str:
    slug = _SLUG_RE.sub("-", value.casefold()).strip("-")
    return slug[:48] or "mesh"


def _iter_bytes(payload: bytes, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
    for offset in range(0, len(payload), chunk_size):
        yield payload[offset : offset + chunk_size]


class MeshImporter:
    """Importer for STL and OBJ files; always produces a visualization GLB."""

    format_name = "mesh"
    suffixes = frozenset({".stl", ".obj"})

    def __init__(self, *, display_length_unit_to_m: float = 0.001) -> None:
        self.display_length_unit_to_m = display_length_unit_to_m

    def probe(self, path: Path) -> float:
        """Return a confidence from the suffix and a bounded content sniff."""
        detected = detect_mesh_format(Path(path))
        if detected is None:
            return 0.0
        score = 0.7
        if Path(path).suffix.lower() in self.suffixes:
            score += 0.3
        return min(score, 1.0)

    def import_file(
        self,
        path: Path,
        *,
        options: ImportOptions,
        resources: ResourceWriter,
    ) -> ImportResult:
        """Map one mesh file to a single-body package with a GLB resource."""
        source_path = Path(path)
        if options.ir_version != CURRENT_IR_VERSION:
            raise MeshImportError(
                f"cad3d-ir mesh importer supports IR {CURRENT_IR_VERSION}, "
                f"not {options.ir_version}"
            )
        if not source_path.is_file():
            raise MeshImportError(f"mesh source is not a file: {source_path}")
        detected = detect_mesh_format(source_path)
        if detected is None:
            raise MeshImportError(f"not an STL or OBJ file: {source_path}")
        data = read_obj(source_path) if detected == "obj" else read_stl(source_path)
        triangles, dropped = drop_degenerate_triangles(data.triangles)
        data.degenerate_triangles = dropped
        if not triangles and not data.polylines:
            raise MeshImportError(f"mesh source contains no geometry: {source_path}")

        digest, byte_length = _hash_file(source_path)
        format_name = "obj" if detected == "obj" else "stl"
        source_id = f"source-{format_name}-{digest[:12]}"
        stem = source_path.stem or format_name
        source = SourceDocument(
            id=source_id,
            name=source_path.name,
            format=format_name,
            version="binary" if detected == "stl-binary" else "ascii",
            uri=source_path.name,
            sha256=digest,
        )
        source_ref = SourceRef(
            document_id=source_id,
            source_kind=detected,
            source_path=source_path.name,
        )
        key = digest[:12]
        product_id = f"product-{_slug(stem)}-{key}"
        configuration_id = f"configuration-{_slug(stem)}-{key}"
        body_id = f"body-{_slug(stem)}-{key}"
        representation_id = f"representation-{_slug(stem)}-{key}"
        resource_id = f"resource-{_slug(stem)}-{key}"
        visualization_id = f"visualization-{_slug(stem)}-{key}"

        builder = GlbBuilder(
            generator=f"cad3d-ir {CURRENT_IR_VERSION} mesh importer",
            root_name=stem,
            length_unit_to_m=self.display_length_unit_to_m,
            up_axis="z",
        )
        face_material = builder.add_material("faces", _FACE_COLOR)
        mesh = builder.add_mesh(stem, extras={"id": body_id})
        if triangles:
            builder.add_triangles(
                mesh,
                positions=triangles,
                indices=list(range(len(triangles) // 3)),
                normals=flat_normals(triangles),
                material=face_material,
                extras={"entityKind": "face", "pickId": 1},
            )
        if data.polylines:
            line_material = builder.add_material("lines", _LINE_COLOR, unlit=True)
            for index, polyline in enumerate(data.polylines, start=2):
                builder.add_lines(
                    mesh,
                    positions=polyline,
                    strip=True,
                    material=line_material,
                    extras={"entityKind": "edge", "pickId": index},
                )
        builder.add_node(stem, mesh=mesh, extras={"id": product_id})
        payload = builder.build()
        bounds = builder.bounds
        resource = resources.write_chunks(
            id=resource_id,
            name=f"{stem} mesh",
            uri=f"geometry/{_slug(stem)}-{key}.glb",
            chunks=_iter_bytes(payload),
            media_type=GLB_MEDIA_TYPE,
            encoding=GLB_ENCODING,
            source=source_ref,
        )
        statistics_counts = {
            "triangles": builder.triangle_count,
            "line_segments": builder.line_segment_count,
            "vertices": builder.vertex_count,
        }
        ir_bounds = BBox3(min=bounds[0], max=bounds[1]) if bounds is not None else None
        representation = GeometryRepresentation(
            id=representation_id,
            name=f"{stem} mesh",
            kind="mesh",
            role="design",
            fidelity="exact",
            resource_id=resource.id,
            bounds=ir_bounds,
            derivation=RepresentationDerivation(
                relation="converted",
                method=f"cad3d-ir mesh importer ({detected} to glTF binary)",
                source=source_ref,
            ),
            source=source_ref,
        )
        package = CadPackage(
            format="cad3d-ir",
            version=CURRENT_IR_VERSION,
            id=f"package-{format_name}-{key}",
            name=stem,
            units=UnitSystem(length_unit="unknown"),
            coordinate_system=CoordinateSystem(),
            source_documents=[source],
            product_definitions=[
                ProductDefinition(
                    id=product_id,
                    name=stem,
                    kind="part",
                    source_document_id=source_id,
                    default_configuration_id=configuration_id,
                    source=source_ref,
                )
            ],
            configurations=[
                Configuration(
                    id=configuration_id,
                    name="Default",
                    product_id=product_id,
                    body_ids=[body_id],
                    source=source_ref,
                )
            ],
            bodies=[
                Body(
                    id=body_id,
                    name=f"{stem} body",
                    representation_ids=[representation_id],
                    source=source_ref,
                )
            ],
            representations=[representation],
            resources=[resource],
            visualizations=[
                Visualization(
                    id=visualization_id,
                    name=f"{stem} preview",
                    kind="glb",
                    scope="package",
                    resource_id=resource.id,
                    units=UnitSystem(length_unit="mm"),
                    bounds=ir_bounds,
                    derivation=RepresentationDerivation(
                        relation="converted",
                        source_representation_id=representation_id,
                        method="cad3d-ir GlbBuilder",
                        source=source_ref,
                    ),
                    statistics=statistics_counts,
                    source=source_ref,
                )
            ],
            roots=[
                RootSelection(product_id=product_id, configuration_id=configuration_id)
            ],
            metadata={
                "adapter": "cad3d-ir mesh importer",
                "adapter_version": CURRENT_IR_VERSION,
                "source_format": detected,
            },
        )

        diagnostics = _diagnostics(data, detected, source_id, dropped)
        if options.validate:
            validate_package(package)
        statistics: dict[str, Any] = {
            "source_byte_length": byte_length,
            "source_format": detected,
            "products": 1,
            "parts": 1,
            "assemblies": 0,
            "occurrences": 0,
            "bodies": 1,
            "resources": 1,
            "visualization_byte_length": len(payload),
            **statistics_counts,
        }
        return ImportResult(
            package=package, diagnostics=diagnostics, statistics=statistics
        )


def _diagnostics(
    data: MeshData,
    detected: MeshFormat,
    source_id: str,
    dropped: int,
) -> list[ImportDiagnostic]:
    diagnostics = [
        ImportDiagnostic(
            code="MESH_UNITS_UNKNOWN",
            severity="info",
            action="preserved",
            message=(
                "The source declares no length unit; the package keeps units "
                "unknown and the visualization assumes millimetres for display."
            ),
            source_document_id=source_id,
        )
    ]
    if dropped:
        diagnostics.append(
            ImportDiagnostic(
                code="MESH_DEGENERATE_TRIANGLES_DROPPED",
                severity="warning",
                action="skipped",
                message=f"Dropped {dropped} zero-area triangles.",
                source_document_id=source_id,
                details={"count": dropped},
            )
        )
    if data.binary_length_mismatch:
        diagnostics.append(
            ImportDiagnostic(
                code="STL_BINARY_LENGTH_MISMATCH",
                severity="warning",
                action="repaired",
                message=(
                    "The binary STL triangle count disagrees with the file length; "
                    "only complete records were read."
                ),
                source_document_id=source_id,
            )
        )
    if detected == "obj":
        if data.polygons_triangulated:
            diagnostics.append(
                ImportDiagnostic(
                    code="OBJ_POLYGONS_TRIANGULATED",
                    severity="info",
                    action="approximated",
                    message=(
                        f"Fan-triangulated {data.polygons_triangulated} faces with "
                        "more than three corners."
                    ),
                    source_document_id=source_id,
                    details={"count": data.polygons_triangulated},
                )
            )
        if data.materials_present:
            diagnostics.append(
                ImportDiagnostic(
                    code="OBJ_MATERIALS_IGNORED",
                    severity="info",
                    action="skipped",
                    message="OBJ material libraries and assignments were ignored.",
                    source_document_id=source_id,
                )
            )
        if data.groups_present:
            diagnostics.append(
                ImportDiagnostic(
                    code="OBJ_GROUPS_FLATTENED",
                    severity="info",
                    action="normalized",
                    message="OBJ object and group names were flattened into one body.",
                    source_document_id=source_id,
                )
            )
    return diagnostics


__all__ = [
    "MeshData",
    "MeshFormat",
    "MeshImportError",
    "MeshImporter",
    "detect_mesh_format",
    "read_obj",
    "read_stl",
]
