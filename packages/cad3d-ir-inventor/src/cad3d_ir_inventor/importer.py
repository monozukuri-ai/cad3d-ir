"""Autodesk Inventor part (IPT) importer for cad3d-ir through inventor-kit.

The adapter reads the part with inventor-kit, converts the bodies of the
stored B-Rep table into exact OCCT shapes (``convert_bodies(backend="occt")``,
millimetres) and writes one glTF binary with the same conventions as the STEP
adapter. Each body is one node; a part has no placements. The geometry is the
table the file stores: whether it is the model state Inventor shows is not
established, and colours (appearances) are not read.
"""

from __future__ import annotations

import hashlib
import re
import struct
import time
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

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
    ProductDefinition,
    RepresentationDerivation,
    RootSelection,
    SourceDocument,
    SourceRef,
    UnitSystem,
    Visualization,
)
from cad3d_ir_step.shapes import (
    IDENTITY,
    Bounds,
    GlbNode,
    brep_bytes,
    build_glb,
    diagonal,
    merge_bounds,
)

from cad3d_ir_inventor.errors import (
    InventorImportError,
    InventorReadError,
    UnsupportedIRVersionError,
)

ADAPTER_NAME = "cad3d-ir-inventor"
ADAPTER_VERSION = "0.1.0"
SOURCE_FORMAT = "ipt"
BREP_MEDIA_TYPE = "application/vnd.opencascade.brep"
BREP_ENCODING = "occt-brep-ascii-v1"
# Inventor documents are OLE2 compound files.
SIGNATURE = bytes.fromhex("d0cf11e0a1b11ae1")
TARGET_UNIT = "mm"

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_IMAGE_SUFFIXES = {"image/png": "png", "image/bmp": "bmp", "image/jpeg": "jpg"}
_PROFILE_CANDIDATE = "candidate.profile_candidate"
_PROFILE_UNSUPPORTED = "candidate.profile_unsupported"
_SHEET_CODES = frozenset({"geometry.shell_open", "geometry.sewing_no_shell"})
_STORED_FIT = "geometry.approximated_from_stored_spline"
# The segment major counts releases: 25 is Inventor 2021, 31 is Inventor 2027.
_RELEASE_OF_MAJOR_OFFSET = 1996
_MAX_PREVIEW_PIXELS = 50_000_000


def inventor_kit_version() -> str:
    try:
        return version("inventor-kit")
    except PackageNotFoundError:  # pragma: no cover - editable checkouts
        return "unknown"


def _kit() -> Any:
    try:
        import inventor_kit
    except ImportError as exc:
        raise InventorImportError(
            "cad3d-ir-inventor needs inventor-kit, which requires Python 3.11 or later"
        ) from exc
    return inventor_kit


def slug(value: str) -> str:
    text = _SLUG_RE.sub("-", value.casefold()).strip("-")
    return text[:48] or "model"


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


def is_compound_file(head: bytes) -> bool:
    """Whether ``head`` starts an OLE2 compound file (every Inventor document)."""
    return head.startswith(SIGNATURE)


def release_of_major(major: int | None) -> str | None:
    """The Inventor release that writes a segment major (``Inventor 2021`` for 25)."""
    if major is None:
        return None
    return f"Inventor {_RELEASE_OF_MAJOR_OFFSET + int(major)}"


def _short(reason: str) -> str:
    text = " ".join(str(reason).split())
    return text if len(text) <= 160 else text[:157] + "..."


@dataclass(frozen=True, slots=True)
class Failure:
    """An inventor-kit failure reduced to a code and its message."""

    code: str
    message: str

    @property
    def limit(self) -> bool:
        return "limit" in self.code

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message}


def _failure(exc: BaseException, default: str) -> Failure:
    message = _short(str(exc))
    return Failure("inventor.limit" if "limit" in message.lower() else default, message)


@dataclass(frozen=True, slots=True)
class PreviewImage:
    """The preview image the document stores, as a complete image file."""

    media_type: str
    suffix: str
    data: bytes
    width: int | None
    height: int | None


@dataclass(slots=True)
class InventorSource:
    """One Inventor document as inventor-kit reads and converts it."""

    path: Path
    digest: str
    byte_length: int
    document: Any | None
    read_error: Failure | None
    conversion: Any | None
    conversion_error: Failure | None
    kernel_missing: bool
    read_seconds: float
    convert_seconds: float

    @property
    def _metadata(self) -> Any | None:
        return None if self.document is None else self.document.metadata

    @property
    def _geometry(self) -> Any | None:
        return None if self.document is None else self.document.geometry

    @property
    def _candidates(self) -> tuple[Any, ...]:
        geometry = self._geometry
        return () if geometry is None else tuple(geometry.candidates)

    @property
    def _selected(self) -> Any | None:
        geometry = self._geometry
        if geometry is None:
            return None
        selected = getattr(geometry.selection, "selected_id", None)
        for candidate in self._candidates:
            if candidate.id == selected:
                return candidate
        return None

    @property
    def kind(self) -> str | None:
        """``part``, ``assembly``, ``drawing`` or ``presentation``."""
        metadata = self._metadata
        if metadata is None or metadata.identification is None:
            return None
        return str(metadata.identification.kind)

    @property
    def _profile(self) -> Any | None:
        metadata = self._metadata
        return None if metadata is None else metadata.geometry_profile

    @property
    def major(self) -> int | None:
        """The segment major of the stored B-Rep (25 = Inventor 2021)."""
        profile = self._profile
        value = None if profile is None else profile.segment_major
        if value is None:
            for candidate in self._candidates:
                carrier = getattr(candidate, "carrier", None)
                value = getattr(carrier, "segment_major", None)
                if value is not None:
                    break
        return None if value is None else int(value)

    @property
    def save_version(self) -> int | None:
        """The save version of the stored kernel data (22600 = ASM 226)."""
        profile = self._profile
        value = None if profile is None else profile.sab_version
        return None if value is None else int(value)

    @property
    def release(self) -> str | None:
        return release_of_major(self.major)

    @property
    def profile_status(self) -> str | None:
        selected = self._selected
        if selected is not None:
            return str(selected.profile_status)
        for candidate in self._candidates:
            return str(candidate.profile_status)
        return None

    def _candidate_codes(self) -> set[str]:
        return {
            str(item.code)
            for candidate in self._candidates
            for item in candidate.diagnostics
        }

    @property
    def candidate_profile(self) -> bool:
        """The save generation is framed like its neighbours, without a fixture."""
        return (
            self.profile_status == "candidate_stored_table"
            or _PROFILE_CANDIDATE in self._candidate_codes()
        )

    @property
    def profile_unsupported(self) -> bool:
        return (
            self.document is not None
            and self.document.model is None
            and (
                self.profile_status == "unsupported"
                or _PROFILE_UNSUPPORTED in self._candidate_codes()
            )
        )

    @property
    def state_verified(self) -> bool:
        return bool(getattr(self.conversion, "current_state_verified", False))

    @property
    def geometry_reasons(self) -> list[str]:
        """Why inventor-kit selected no stored table (empty when it did)."""
        if self.document is None or self.document.model is not None:
            return []
        return [_short(item) for item in self.document.summary.get("diagnostics", ())]

    @property
    def properties(self) -> dict[str, Any]:
        """The iProperties inventor-kit names (``part_number``, ``title``, ...)."""
        metadata = self._metadata
        result: dict[str, Any] = {}
        if metadata is None:
            return result
        for property_set in metadata.property_sets:
            for item in property_set.properties:
                name = item.semantic_name
                value = item.value
                if (
                    name
                    and name not in result
                    and isinstance(value, (str, int, float, bool))
                    and value != ""
                ):
                    result[str(name)] = value
        return result

    @property
    def thumbnail(self) -> PreviewImage | None:
        """The first stored preview image that is a complete image file."""
        metadata = self._metadata
        if metadata is None:
            return None
        for item in metadata.thumbnails:
            suffix = _IMAGE_SUFFIXES.get(str(item.mime_type))
            data = bytes(item.data)
            if suffix is None or not data:
                continue
            width, height = item.width, item.height
            if suffix == "png":
                if len(data) < 24 or not data.startswith(_PNG_SIGNATURE):
                    continue
                width, height = struct.unpack_from(">II", data, 16)
            if width and height and width * height > _MAX_PREVIEW_PIXELS:
                continue
            return PreviewImage(str(item.mime_type), suffix, data, width, height)
        return None


def read_source(
    path: Path | str,
    *,
    limits: Any | None = None,
    convert: bool = True,
) -> InventorSource:
    """Read one Inventor document and (optionally) convert its bodies to OCCT.

    ``limits`` is an ``inventor_kit.Limits`` that tightens the parser's
    ceilings. A file inventor-kit cannot read, a document without part
    geometry and a missing geometry kernel are kept on the result
    (``read_error``, ``geometry_reasons``, ``kernel_missing``); only a path
    that is not an OLE2 compound file raises.
    """
    source_path = Path(path)
    if not source_path.is_file():
        raise InventorReadError(f"Inventor source is not a file: {source_path}")
    kit = _kit()
    digest, byte_length = hash_file(source_path)
    with source_path.open("rb") as stream:
        head = stream.read(len(SIGNATURE))
    if not is_compound_file(head):
        raise InventorReadError(
            f"not an Inventor document (no OLE2 compound file header): {source_path}"
        )

    document: Any | None = None
    read_error: Failure | None = None
    started = time.perf_counter()
    try:
        document = kit.read_file(source_path, limits=limits)
    except (ValueError, OSError) as exc:
        read_error = _failure(exc, "inventor.unreadable")
    read_seconds = time.perf_counter() - started

    conversion: Any | None = None
    conversion_error: Failure | None = None
    kernel_missing = False
    convert_seconds = 0.0
    if document is not None and document.model is not None and convert:
        started = time.perf_counter()
        try:
            conversion = document.convert_bodies(backend="occt")
        except ImportError as exc:
            kernel_missing = True
            conversion_error = Failure("inventor.kernel_missing", _short(str(exc)))
        except (ValueError, RuntimeError) as exc:
            conversion_error = _failure(exc, "inventor.conversion_failed")
        convert_seconds = time.perf_counter() - started
    return InventorSource(
        path=source_path,
        digest=digest,
        byte_length=byte_length,
        document=document,
        read_error=read_error,
        conversion=conversion,
        conversion_error=conversion_error,
        kernel_missing=kernel_missing,
        read_seconds=read_seconds,
        convert_seconds=convert_seconds,
    )


# -- classification -----------------------------------------------------------------


@dataclass(slots=True)
class BodyRecord:
    """One stored body with its converted shape and outcome.

    ``status`` is ``converted`` (a valid solid), ``sheet`` (faces that do not
    close a volume), ``unsupported`` (geometry the converter does not build)
    or ``failed``. ``reason`` is the converter's message; ``reason_key`` the
    geometry subtype when it names one, else the diagnostic code.
    """

    index: int
    id: str
    shape: Any | None
    status: str
    code: str | None
    reason: str | None
    reason_key: str | None
    face_count: int
    volume_mm3: float | None
    area_mm2: float | None
    bounds: Bounds | None
    warnings: tuple[str, ...]
    approximations: tuple[dict[str, Any], ...]

    @property
    def key(self) -> str:
        return f"body-{self.index}"

    @property
    def label(self) -> str:
        return f"Body_{self.index}"

    @property
    def exact(self) -> bool:
        """No stored B-spline fit stands in for the body's geometry."""
        return not self.approximations


@dataclass(slots=True)
class Classification:
    """What each stored body contributes."""

    bodies: list[BodyRecord] = field(default_factory=list)
    unsupported_reasons: Counter[str] = field(default_factory=Counter)

    @property
    def converted(self) -> list[BodyRecord]:
        return [body for body in self.bodies if body.shape is not None]

    def with_status(self, status: str) -> list[BodyRecord]:
        return [body for body in self.bodies if body.status == status]


def _bounds(metrics: dict[str, Any] | None) -> Bounds | None:
    box = None if metrics is None else metrics.get("bbox_mm")
    if not box or len(box) != 6:
        return None
    return (
        (float(box[0]), float(box[1]), float(box[2])),
        (float(box[3]), float(box[4]), float(box[5])),
    )


def classify(source: InventorSource) -> Classification:
    """Pair every stored body with its converted shape or the reason it has none."""
    result = Classification()
    if source.conversion is None:
        return result
    for body in source.conversion.bodies:
        metrics = body.metrics or {}
        first = body.diagnostics[0] if body.diagnostics else None
        code = None if first is None else str(first.code)
        if body.status == "converted_solid" and body.shape is not None:
            status = "converted"
        elif body.status == "open_shell" or code in _SHEET_CODES:
            status = "sheet"
        elif body.status == "unsupported":
            status = "unsupported"
        else:
            status = "failed"
        reason_key = None
        if first is not None:
            reason_key = str(getattr(first, "subtype", None) or code)
        if status in {"unsupported", "failed"} and reason_key is not None:
            result.unsupported_reasons[reason_key] += 1
        result.bodies.append(
            BodyRecord(
                index=int(body.body_index),
                id=str(body.id),
                shape=body.shape if status == "converted" else None,
                status=status,
                code=code,
                reason=None if first is None else _short(first.message),
                reason_key=reason_key,
                face_count=int(metrics.get("faces", 0)),
                volume_mm3=metrics.get("volume_mm3"),
                area_mm2=metrics.get("area_mm2"),
                bounds=_bounds(body.metrics) if status == "converted" else None,
                warnings=tuple(_short(item.message) for item in body.warnings),
                approximations=tuple(dict(item) for item in body.approximations),
            )
        )
    return result


# -- mapping --------------------------------------------------------------------------


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


class InventorImporter:
    """Importer for Autodesk Inventor parts (``.ipt``).

    ``limits`` is an ``inventor_kit.Limits`` that tightens the parser's
    ceilings. Assemblies, drawings and presentations are identified and
    reported, not converted. The adapter needs ``inventor-kit[occt]``, which
    the distribution requires on Python 3.11 and later.
    """

    format_name = SOURCE_FORMAT
    suffixes = frozenset({".ipt"})

    def __init__(self, *, limits: Any | None = None) -> None:
        self.limits = limits

    @property
    def accepted_suffixes(self) -> frozenset[str]:
        return self.suffixes

    def probe(self, path: Path) -> float:
        """Return a bounded confidence from the compound-file header and the suffix.

        The header is shared with other OLE2 documents, so a file without the
        ``.ipt`` suffix gets a low confidence.
        """
        source_path = Path(path)
        try:
            if not source_path.is_file():
                return 0.0
            with source_path.open("rb") as stream:
                head = stream.read(len(SIGNATURE))
        except OSError:
            return 0.0
        if not is_compound_file(head):
            return 0.0
        return 0.9 if source_path.suffix.lower() in self.suffixes else 0.2

    def import_file(
        self,
        path: Path,
        *,
        options: ImportOptions,
        resources: ResourceWriter,
    ) -> ImportResult:
        """Map one Inventor part to a package with its stored bodies."""
        if options.ir_version != CURRENT_IR_VERSION:
            raise UnsupportedIRVersionError(
                f"{ADAPTER_NAME} supports IR {CURRENT_IR_VERSION}, "
                f"not {options.ir_version}"
            )
        source = read_source(path, limits=self.limits)
        return map_source(source, options=options, resources=resources)


def map_source(
    source: InventorSource,
    *,
    options: ImportOptions,
    resources: ResourceWriter,
) -> ImportResult:
    """Map an already read :class:`InventorSource` to an :class:`ImportResult`."""
    key = source.digest[:12]
    stem = source.path.stem or "model"
    name_slug = slug(stem)
    source_id = f"source-{SOURCE_FORMAT}-{key}"
    profile = {
        "segment_major": source.major,
        "save_version": source.save_version,
        "release": source.release,
        "profile_status": source.profile_status,
    }
    source_ref = SourceRef(
        document_id=source_id,
        source_kind="inventor-document",
        source_id=None if source.major is None else f"segment-major-{source.major}",
    )
    diagnostics: list[ImportDiagnostic] = []
    classification = classify(source)
    converted = classification.converted
    sheets = classification.with_status("sheet")
    unsupported = classification.with_status("unsupported")
    failed = classification.with_status("failed")
    thumbnail = source.thumbnail
    release = source.release or "an unknown Inventor release"

    # Read and conversion state --------------------------------------------------
    blocked = True
    if source.read_error is not None:
        failure = source.read_error
        diagnostics.append(
            _diagnostic(
                "INVENTOR_LIMIT" if failure.limit else "INVENTOR_READ_FAILED",
                "error",
                "unresolved",
                (
                    f"inventor-kit stopped at a resource limit: {failure.message}"
                    if failure.limit
                    else f"inventor-kit could not read the document: {failure.message}"
                ),
                source_id,
                failure.as_dict(),
            )
        )
    elif source.kind is not None and source.kind != "part":
        diagnostics.append(
            _diagnostic(
                "INVENTOR_KIND_UNSUPPORTED",
                "error",
                "unresolved",
                (
                    f"The document is an Inventor {source.kind}; only the stored "
                    "geometry of parts is read."
                ),
                source_id,
                {"kind": source.kind},
            )
        )
    elif source.profile_unsupported:
        diagnostics.append(
            _diagnostic(
                "INVENTOR_SEGMENT_MAJOR_UNSUPPORTED",
                "error",
                "unresolved",
                (
                    f"The save generation of this part (segment major "
                    f"{source.major if source.major is not None else '?'}, {release}) "
                    "is outside the generations inventor-kit reads."
                ),
                source_id,
                {**profile, "reasons": source.geometry_reasons[:8]},
            )
        )
    elif source.document is not None and source.document.model is None:
        diagnostics.append(
            _diagnostic(
                "INVENTOR_GEOMETRY_UNAVAILABLE",
                "error",
                "unresolved",
                (
                    "The stored B-Rep of the part could not be extracted"
                    + (
                        f": {'; '.join(source.geometry_reasons[:3])}"
                        if source.geometry_reasons
                        else "."
                    )
                ),
                source_id,
                {**profile, "reasons": source.geometry_reasons[:8]},
            )
        )
    elif source.kernel_missing:
        diagnostics.append(
            _diagnostic(
                "INVENTOR_KERNEL_MISSING",
                "error",
                "unresolved",
                (
                    "The geometry kernel is not installed (inventor-kit's occt "
                    "extra); the bodies cannot be converted."
                ),
                source_id,
            )
        )
    elif source.conversion_error is not None:
        failure = source.conversion_error
        diagnostics.append(
            _diagnostic(
                "INVENTOR_LIMIT" if failure.limit else "INVENTOR_CONVERSION_FAILED",
                "error",
                "unresolved",
                f"The bodies could not be converted to OCCT: {failure.message}",
                source_id,
                {**profile, **failure.as_dict()},
            )
        )
    else:
        blocked = False
    if source.candidate_profile:
        diagnostics.append(
            _diagnostic(
                "INVENTOR_PROFILE_CANDIDATE",
                "warning",
                "preserved",
                (
                    f"The save generation of this part (segment major {source.major}, "
                    f"{release}) is read as a candidate: it is framed like the "
                    "verified generations next to it."
                ),
                source_id,
                profile,
            )
        )

    # Geometry ------------------------------------------------------------------------
    world_bounds: Bounds | None = None
    for body in converted:
        world_bounds = merge_bounds(world_bounds, body.bounds)
    linear_deflection = max(
        diagonal(world_bounds) * options.mesh_linear_deflection_ratio, 1e-6
    )
    shapes_by_key = {body.key: body.shape for body in converted}
    root_product_id = f"product-{name_slug}-{key}"
    root_configuration_id = f"configuration-{name_slug}-{key}"
    ir_body_id = {body.index: f"body-{body.index}-{key}" for body in converted}
    glb_nodes = [
        GlbNode(
            name=body.label,
            parent=None,
            matrix=IDENTITY,
            key=body.key,
            extras={
                "id": ir_body_id[body.index],
                "product": root_product_id,
                "body": ir_body_id[body.index],
            },
        )
        for body in converted
    ]

    resource_records: list[Any] = []
    representations: list[GeometryRepresentation] = []
    visualizations: list[Visualization] = []
    representation_of_body: dict[int, str] = {}
    glb_result = None
    if converted and options.include_visualization_mesh:
        glb_result = build_glb(
            glb_nodes,
            shapes_by_key,
            linear_deflection=linear_deflection,
            angular_deflection=options.mesh_angular_deflection,
            include_edges=options.mesh_include_edges,
            generator=f"{ADAPTER_NAME} {ADAPTER_VERSION}",
            root_name=stem,
            root_extras={
                "cad3d_ir_inventor": {
                    "source_format": SOURCE_FORMAT,
                    "inventor_kit_version": inventor_kit_version(),
                    "segment_major": source.major,
                    "save_version": source.save_version,
                    "linear_deflection_mm": linear_deflection,
                    "angular_deflection_rad": options.mesh_angular_deflection,
                }
            },
        )
        if glb_result.failed_keys:
            diagnostics.append(
                _diagnostic(
                    "INVENTOR_MESH_FAILED",
                    "warning",
                    "skipped",
                    (
                        f"{len(glb_result.failed_keys)} converted bodies could not be "
                        "tessellated and are left out of the visualization."
                    ),
                    source_id,
                    {"bodies": dict(list(glb_result.failed_keys.items())[:20])},
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
            properties={"inventor_kit_version": inventor_kit_version()},
        )
        resource_records.append(resource)
        placed = glb_result.placed_bounds or world_bounds
        visualizations.append(
            Visualization(
                id=f"visualization-{name_slug}-{key}",
                name=f"{stem} visualization",
                kind="glb",
                scope="package",
                resource_id=resource.id,
                units=UnitSystem(length_unit="mm"),
                bounds=None if placed is None else BBox3(min=placed[0], max=placed[1]),
                derivation=RepresentationDerivation(
                    relation="tessellated",
                    method=(
                        "inventor-kit stored B-Rep to OCCT through OCCT "
                        "BRepMesh_IncrementalMesh to glTF binary"
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
                    "materials": glb_result.material_count,
                    "bodies": len(converted),
                },
                source=source_ref,
            )
        )
    if converted and options.include_brep:
        for position, body in enumerate(converted):
            payload = brep_bytes(body.shape)
            brep_resource = resources.write_chunks(
                id=f"resource-brep-{body.index}-{key}",
                name=f"{body.label} B-Rep",
                uri=f"geometry/{name_slug}-{key}-{position}.brep",
                chunks=_iter_bytes(payload),
                media_type=BREP_MEDIA_TYPE,
                encoding=BREP_ENCODING,
                source=source_ref,
                properties={
                    "inventor_kit_version": inventor_kit_version(),
                    "frame": "body",
                },
            )
            resource_records.append(brep_resource)
            representation_id = f"representation-brep-{body.index}-{key}"
            representations.append(
                GeometryRepresentation(
                    id=representation_id,
                    name=f"{body.label} B-Rep",
                    kind="brep",
                    role="design",
                    fidelity="exact" if body.exact else "approximate",
                    resource_id=brep_resource.id,
                    units=UnitSystem(length_unit="mm"),
                    bounds=(
                        None
                        if body.bounds is None
                        else BBox3(min=body.bounds[0], max=body.bounds[1])
                    ),
                    derivation=RepresentationDerivation(
                        relation="converted",
                        method=(
                            "inventor-kit convert_bodies (cq-acis) to OCCT "
                            "BRepTools ASCII"
                        ),
                        source=source_ref,
                        metadata={
                            "status": body.status,
                            "stored_fits": len(body.approximations),
                        },
                    ),
                    source=source_ref,
                )
            )
            representation_of_body[body.index] = representation_id

    # Package objects ---------------------------------------------------------------
    bodies: list[Body] = []
    for body in converted:
        properties: dict[str, Any] = {
            "inventor_body_index": body.index,
            "inventor_kit_body_id": body.id,
            "status": body.status,
            "faces": body.face_count,
            "volume_mm3": body.volume_mm3,
            "area_mm2": body.area_mm2,
            "stored_fits": len(body.approximations),
        }
        bodies.append(
            Body(
                id=ir_body_id[body.index],
                name=body.label,
                representation_ids=(
                    [representation_of_body[body.index]]
                    if body.index in representation_of_body
                    else []
                ),
                source=SourceRef(
                    document_id=source_id,
                    source_kind="inventor-body",
                    source_id=str(body.index),
                    metadata={"status": body.status},
                ),
                properties=properties,
            )
        )
    iproperties = source.properties
    product_definitions = [
        ProductDefinition(
            id=root_product_id,
            name=stem,
            kind="part",
            source_document_id=source_id,
            default_configuration_id=root_configuration_id,
            source=source_ref,
            properties={
                key_: iproperties[key_]
                for key_ in ("part_number", "title")
                if key_ in iproperties
            },
        )
    ]
    configurations = [
        Configuration(
            id=root_configuration_id,
            name="Stored state",
            product_id=root_product_id,
            body_ids=[ir_body_id[body.index] for body in converted],
            occurrence_ids=[],
            source=source_ref,
            properties={"state_verified": source.state_verified},
        )
    ]

    # Remaining diagnostics ---------------------------------------------------------
    approximations = [item for body in converted for item in body.approximations]
    approximated_curves = sum(
        str(item.get("type", "")).endswith("curve") for item in approximations
    )
    approximated_surfaces = len(approximations) - approximated_curves
    largest_fit = max(
        (float(item.get("fit_tolerance_mm") or 0.0) for item in approximations),
        default=0.0,
    )
    if converted:
        diagnostics.append(
            _diagnostic(
                "INVENTOR_EXACT_BREP",
                "info",
                "preserved",
                (
                    "Geometry is the B-Rep the part stores, converted to OCCT; "
                    "exact surfaces are kept."
                ),
                source_id,
                {
                    "bodies_converted": len(converted),
                    "bodies_with_stored_fits": sum(not b.exact for b in converted),
                },
            )
        )
        if not source.state_verified:
            diagnostics.append(
                _diagnostic(
                    "INVENTOR_STATE_UNVERIFIED",
                    "info",
                    "preserved",
                    (
                        "The bodies are the table the file stores; that it is the "
                        "model state Inventor shows (suppression, other model "
                        "states) is not established."
                    ),
                    source_id,
                )
            )
        diagnostics.append(
            _diagnostic(
                "INVENTOR_APPEARANCE_NOT_READ",
                "info",
                "skipped",
                "Appearances (colours) are not read; bodies use the default colour.",
                source_id,
            )
        )
    if approximations:
        diagnostics.append(
            _diagnostic(
                "INVENTOR_APPROXIMATED_GEOMETRY",
                "info",
                "approximated",
                (
                    f"{approximated_curves} curves and {approximated_surfaces} "
                    "surfaces defined by a procedure are represented by the "
                    "B-spline fits Inventor stored with them (largest fit "
                    f"tolerance {largest_fit:.3g} mm)."
                ),
                source_id,
                {
                    "curves": approximated_curves,
                    "surfaces": approximated_surfaces,
                    "largest_fit_tolerance_mm": largest_fit,
                    "subtypes": dict(
                        Counter(
                            str(item.get("subtype") or item.get("type") or "unknown")
                            for item in approximations
                        ).most_common(12)
                    ),
                },
            )
        )
    if unsupported:
        diagnostics.append(
            _diagnostic(
                "INVENTOR_BODY_UNSUPPORTED",
                "warning",
                "skipped",
                (
                    f"{len(unsupported)} of {len(classification.bodies)} bodies hold "
                    "geometry that is not converted and are left out."
                ),
                source_id,
                {
                    "bodies": len(unsupported),
                    "reasons": dict(
                        Counter(
                            body.reason_key or "unknown" for body in unsupported
                        ).most_common(12)
                    ),
                },
            )
        )
    if sheets:
        diagnostics.append(
            _diagnostic(
                "INVENTOR_BODY_SHEET",
                "info",
                "skipped",
                (
                    f"{len(sheets)} bodies are faces that do not close a volume "
                    "(construction surfaces) and are left out."
                ),
                source_id,
                {"bodies": [body.index for body in sheets[:50]]},
            )
        )
    if failed:
        diagnostics.append(
            _diagnostic(
                "INVENTOR_BODY_FAILED",
                "warning",
                "skipped",
                (
                    f"{len(failed)} of {len(classification.bodies)} bodies could not "
                    "be built and are left out."
                ),
                source_id,
                {
                    "bodies": len(failed),
                    "reasons": dict(
                        Counter(
                            body.reason_key or "unknown" for body in failed
                        ).most_common(12)
                    ),
                    "messages": [body.reason for body in failed[:8]],
                },
            )
        )
    if not converted and not blocked:
        diagnostics.append(
            _diagnostic(
                "INVENTOR_NO_GEOMETRY",
                "error",
                "unresolved",
                "No body of the part yielded a displayable shape.",
                source_id,
                {
                    "bodies": len(classification.bodies),
                    "reasons": dict(classification.unsupported_reasons.most_common(12)),
                },
            )
        )
    if not converted and thumbnail is not None:
        diagnostics.append(
            _diagnostic(
                "INVENTOR_PREVIEW_ONLY",
                "warning",
                "skipped",
                (
                    "No shape can be displayed; the preview image saved with the "
                    "document and its properties are available."
                ),
                source_id,
            )
        )
    if options.strict and not converted:
        if source.read_error is not None:
            reason = "unreadable document"
        elif source.kind is not None and source.kind != "part":
            reason = f"not a part ({source.kind})"
        elif source.profile_unsupported:
            reason = "unsupported save generation"
        elif source.kernel_missing:
            reason = "missing geometry kernel"
        else:
            reason = "no displayable body"
        raise InventorReadError(f"{reason} in Inventor document: {source.path}")

    # Package --------------------------------------------------------------------
    metadata: dict[str, Any] = {
        "adapter": ADAPTER_NAME,
        "adapter_version": ADAPTER_VERSION,
        "inventor_kit_version": inventor_kit_version(),
        "source_format": SOURCE_FORMAT,
        "kind": source.kind,
        "segment_major": source.major,
        "save_version": source.save_version,
        "release": source.release,
        "candidate_profile": source.candidate_profile,
        "state_verified": source.state_verified,
    }
    preview_length = 0
    if thumbnail is not None:
        preview_record = resources.write_chunks(
            id=f"resource-preview-{name_slug}-{key}",
            name=f"{stem} preview image",
            uri=f"preview/{name_slug}-{key}.{thumbnail.suffix}",
            chunks=(thumbnail.data,),
            media_type=thumbnail.media_type,
            encoding=thumbnail.suffix,
            source=SourceRef(
                document_id=source_id,
                source_kind="inventor-thumbnail",
                source_id=thumbnail.media_type,
            ),
            properties={
                "role": "preview-image",
                "width": thumbnail.width,
                "height": thumbnail.height,
            },
        )
        resource_records.append(preview_record)
        preview_length = len(thumbnail.data)
        metadata["preview_resource_id"] = preview_record.id
    source_document = SourceDocument(
        id=source_id,
        name=source.path.name,
        format=SOURCE_FORMAT,
        version=source.release,
        uri=source.path.name,
        sha256=source.digest,
        properties={
            "kind": source.kind,
            "segment_major": source.major,
            "save_version": source.save_version,
            "profile_status": source.profile_status,
            "candidate_profile": source.candidate_profile,
            "state_verified": source.state_verified,
            "iproperties": iproperties,
            "units": TARGET_UNIT,
        },
    )
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
        occurrences=[],
        bodies=bodies,
        representations=representations,
        resources=resource_records,
        visualizations=visualizations,
        roots=[
            RootSelection(
                product_id=root_product_id, configuration_id=root_configuration_id
            )
        ],
        metadata=metadata,
    )
    if options.validate:
        validate_package(package)

    statistics: dict[str, Any] = {
        "source_byte_length": source.byte_length,
        "source_format": SOURCE_FORMAT,
        "kind": source.kind,
        "major": source.major,
        "save_version": source.save_version,
        "release": source.release,
        "profile_status": source.profile_status,
        "candidate_profile": source.candidate_profile,
        "state_verified": source.state_verified,
        "read_failed": source.read_error is not None,
        "profile_unsupported": source.profile_unsupported,
        "kernel_missing": source.kernel_missing,
        "bodies": len(classification.bodies),
        "bodies_converted": len(converted),
        "bodies_open_shell": len(sheets),
        "bodies_unsupported": len(unsupported),
        "bodies_failed": len(failed),
        "unsupported_reasons": dict(classification.unsupported_reasons),
        "approximated_curves": approximated_curves,
        "approximated_surfaces": approximated_surfaces,
        "largest_fit_tolerance_mm": largest_fit,
        "faces": sum(body.face_count for body in converted),
        "unique_meshes": len(shapes_by_key),
        "triangles": 0 if glb_result is None else glb_result.triangle_count,
        "line_segments": 0 if glb_result is None else glb_result.line_segment_count,
        "vertices": 0 if glb_result is None else glb_result.vertex_count,
        "nodes": 0 if glb_result is None else glb_result.node_count,
        "materials": 0 if glb_result is None else glb_result.material_count,
        "visualization_byte_length": 0
        if glb_result is None
        else len(glb_result.payload),
        "preview_byte_length": preview_length,
        "linear_deflection_mm": linear_deflection,
        "read_seconds": round(source.read_seconds, 3),
        "convert_seconds": round(source.convert_seconds, 3),
    }
    return ImportResult(package=package, diagnostics=diagnostics, statistics=statistics)


__all__ = [
    "ADAPTER_NAME",
    "ADAPTER_VERSION",
    "BREP_ENCODING",
    "BREP_MEDIA_TYPE",
    "SIGNATURE",
    "BodyRecord",
    "Classification",
    "Failure",
    "InventorImporter",
    "InventorSource",
    "PreviewImage",
    "classify",
    "inventor_kit_version",
    "is_compound_file",
    "map_source",
    "read_source",
    "release_of_major",
]
