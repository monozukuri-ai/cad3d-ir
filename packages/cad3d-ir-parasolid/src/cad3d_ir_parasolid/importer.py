"""Parasolid transmit-file (X_T / X_B) importer for cad3d-ir through parasolid-kit.

The adapter parses the transmit file with parasolid-kit, converts its bodies
into exact OCCT shapes (``to_occt``, metres to millimetres, dropping the faces
whose construction fails), places them by the assembly instances the file
stores and writes one glTF binary with the same conventions as the STEP
adapter. Colours and names come from the system attributes
(``SDL/TYSA_COLOUR``, ``SDL/TYSA_NAME``); the other attributes are reported
but not mapped.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version
from math import isfinite
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
from parasolid_kit import (
    InstancePartKind,
    ParasolidError,
    ParseLimits,
    inspect_xb,
    inspect_xt,
    read_brep,
)
from parasolid_kit.interop.errors import InteropDependencyError, InteropError
from parasolid_kit.interop.limits import InteropLimits
from parasolid_kit.interop.occt import to_occt

from cad3d_ir_parasolid.errors import ParasolidReadError, UnsupportedIRVersionError
from cad3d_ir_parasolid.shapes import (
    IDENTITY,
    Bounds,
    GlbNode,
    Matrix4,
    Rgba,
    brep_bytes,
    build_glb,
    diagonal,
    face_pick_ids,
    matmul,
    merge_bounds,
    shape_bounds,
    transform_bounds,
)

ADAPTER_NAME = "cad3d-ir-parasolid"
ADAPTER_VERSION = "0.3.1"
SOURCE_FORMAT = "x_t"
BREP_MEDIA_TYPE = "application/vnd.opencascade.brep"
BREP_ENCODING = "occt-brep-ascii-v1"
# Every transmit file, text or binary, opens with this line.
SIGNATURE = b"**ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz**"
# Transmit files store lengths in metres; the package is in millimetres.
SOURCE_UNIT = "m"
TARGET_UNIT = "mm"
SOURCE_TO_TARGET = 1000.0

_HEADER_BYTES = 4096
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_HEADER_LINE_RE = re.compile(r"^([A-Z_][A-Z0-9_]*)=(.*?);?\s*$")
_COLOUR_PREFIX = "SDL/TYSA_COLOUR"
_NAME_IDENTIFIERS = ("SDL/TYSA_NAME", "SDL/TYSA_UNAME")
_MAX_DEPTH = 64


def parasolid_kit_version() -> str:
    try:
        return version("parasolid-kit")
    except PackageNotFoundError:  # pragma: no cover - editable checkouts
        return "unknown"


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


_TEXT_STREAM_RE = re.compile(rb"^T\d+ : TRANSMIT FILE ")
_BINARY_STREAM = b"PS\0\0"


def is_parasolid_signature(head: bytes) -> bool:
    """True for the ``**ABCDEF...xyz**`` first line shared by X_T and X_B files.

    A bare stream without the common header (the text ``T<n> : TRANSMIT FILE``
    line or the binary ``PS`` marker first) is accepted too; parasolid-kit
    reads both.
    """
    return (
        head.startswith(SIGNATURE)
        or head.startswith(b"**PART")
        or head.startswith(_BINARY_STREAM)
        or _TEXT_STREAM_RE.match(head) is not None
    )


def transmit_encoding(path: Path, head: bytes) -> str:
    """``x-t`` or ``x-b`` for parasolid-kit's ``source_format``.

    The suffix decides (``.x_t`` / ``.xmt_txt`` text, ``.x_b`` / ``.xmt_bin``
    binary); a renamed file is binary when the ``PS`` stream marker follows the
    common header, text otherwise.
    """
    suffix = path.suffix.lower()
    if suffix in {".x_t", ".xmt_txt"}:
        return "x-t"
    if suffix in {".x_b", ".xmt_bin"}:
        return "x-b"
    return "x-b" if _BINARY_STREAM in head else "x-t"


def stream_schema_key(path: Path, encoding: str) -> str | None:
    """The ``SCH_<modeller>_<schema>[_<base>]`` key of the stream itself.

    parasolid-kit's header inspection reads it without any schema, so it is
    available for a file whose schema no profile covers. The common header's
    ``SCH=`` line is not the same thing: it names the writer's native schema
    (Onshape writes ``SCH_3701212_37102`` there while the stream carries the
    older key chosen at export).
    """
    inspect = inspect_xb if encoding == "x-b" else inspect_xt
    try:
        key = inspect(path).schema_key
    except Exception:  # noqa: BLE001 - a header parasolid-kit cannot read has no key
        return None
    return str(getattr(key, "raw", key)) or None


def common_header(head: bytes) -> dict[str, str]:
    """The ``KEY=VALUE;`` lines of the common header (``**PART1`` to ``**PART3``).

    The writer's application (``APPL``), Parasolid version (``FRU``) and the
    schema it names (``SCH``) are the useful ones; the lines of the text or
    binary stream after ``**END_OF_HEADER`` are not read here.
    """
    text = head.decode("latin-1", errors="replace")
    if "**PART1" not in text:
        return {}
    body = text.split("**PART1", 1)[1]
    for stop in ("**END_OF_HEADER", "**PART3"):
        body = body.split(stop, 1)[0]
    fields: dict[str, str] = {}
    for line in body.splitlines():
        match = _HEADER_LINE_RE.match(line.strip())
        if match is not None:
            fields[match.group(1)] = match.group(2).strip()
    return fields


@dataclass(frozen=True, slots=True)
class ParasolidLimits:
    """Bounds handed to parasolid-kit's parser and converter (server-sized)."""

    max_file_size: int = 256 * 1024 * 1024
    max_nodes: int = 10_000_000
    max_variable_elements: int = 10_000_000
    max_entities: int = 1_000_000
    max_occt_subshapes: int = 2_000_000
    max_vertices: int = 10_000_000
    max_diagnostics: int = 10_000

    def parse_limits(self) -> ParseLimits:
        return ParseLimits(
            max_file_size=self.max_file_size,
            max_nodes=self.max_nodes,
            max_variable_elements=self.max_variable_elements,
            max_diagnostics=self.max_diagnostics,
        )

    def interop_limits(self) -> InteropLimits:
        return InteropLimits(
            max_entities=self.max_entities,
            max_occt_subshapes=self.max_occt_subshapes,
            max_vertices=self.max_vertices,
            max_diagnostics=self.max_diagnostics,
        )


@dataclass(frozen=True, slots=True)
class Failure:
    """A parasolid-kit failure reduced to its diagnostic code and message."""

    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def schema(self) -> bool:
        return self.code.startswith("schema.")

    @property
    def limit(self) -> bool:
        return "limit" in self.code

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.details}


def _failure(exc: BaseException) -> Failure:
    diagnostic = getattr(exc, "diagnostic", None)
    if diagnostic is None:
        return Failure("parasolid.unreadable", str(exc))
    details: dict[str, Any] = {}
    for name in ("node_type", "node_id", "schema_key"):
        value = getattr(diagnostic, name, None)
        if value is not None:
            details[name] = value
    return Failure(str(diagnostic.code), str(diagnostic.message), details)


@dataclass(slots=True)
class ParasolidSource:
    """One transmit file as parasolid-kit reads and converts it."""

    path: Path
    digest: str
    byte_length: int
    header: dict[str, str]
    parsed: Any | None
    parse_error: Failure | None
    conversion: Any | None
    conversion_error: Failure | None
    kernel_missing: bool
    read_seconds: float
    convert_seconds: float
    # The stream's key when the parse failed (``stream_schema_key``); the
    # parsed document carries it otherwise.
    transmit_key: str | None = None

    @property
    def encoding(self) -> str | None:
        return None if self.parsed is None else str(self.parsed.document.format)

    @property
    def schema_key(self) -> str | None:
        if self.parsed is not None:
            return str(self.parsed.document.schema_key.raw)
        return self.transmit_key or self.header.get("SCH") or None

    @property
    def modeller_version(self) -> str | None:
        if self.parsed is None:
            return None
        return str(self.parsed.document.header.modeller_version)

    @property
    def application(self) -> str | None:
        return self.header.get("APPL") or None

    @property
    def parasolid_version(self) -> str | None:
        return self.header.get("FRU") or None

    @property
    def schema_profile(self) -> str | None:
        resolution = (
            None if self.parsed is None else self.parsed.document.schema_resolution
        )
        return None if resolution is None else resolution.profile_id

    @property
    def schema_coverage(self) -> str | None:
        resolution = (
            None if self.parsed is None else self.parsed.document.schema_resolution
        )
        return None if resolution is None else resolution.coverage

    @property
    def family_match(self) -> bool:
        return self.schema_coverage == "family_inferred"


def read_source(
    path: Path | str,
    *,
    limits: ParasolidLimits | None = None,
    convert: bool = True,
    partial: bool = True,
) -> ParasolidSource:
    """Parse one transmit file and (optionally) convert its bodies to OCCT."""
    source_path = Path(path)
    if not source_path.is_file():
        raise ParasolidReadError(f"Parasolid source is not a file: {source_path}")
    limits = ParasolidLimits() if limits is None else limits
    digest, byte_length = hash_file(source_path)
    with source_path.open("rb") as stream:
        head = stream.read(_HEADER_BYTES)
    if not is_parasolid_signature(head):
        raise ParasolidReadError(
            f"not a Parasolid transmit file (no **ABCDEF... header line): {source_path}"
        )
    header = common_header(head)

    parsed: Any | None = None
    parse_error: Failure | None = None
    transmit_key: str | None = None
    encoding = transmit_encoding(source_path, head)
    started = time.perf_counter()
    try:
        parsed = read_brep(
            source_path, source_format=encoding, limits=limits.parse_limits()
        )
    except ParasolidError as exc:
        parse_error = _failure(exc)
    except (ValueError, OSError) as exc:
        parse_error = Failure("parasolid.unreadable", str(exc))
    if parsed is None:
        transmit_key = stream_schema_key(source_path, encoding)
    read_seconds = time.perf_counter() - started

    conversion: Any | None = None
    conversion_error: Failure | None = None
    kernel_missing = False
    convert_seconds = 0.0
    if parsed is not None and convert:
        started = time.perf_counter()
        try:
            conversion = to_occt(
                parsed.brep,
                source_unit=SOURCE_UNIT,
                target_unit=TARGET_UNIT,
                require_complete=False,
                partial=partial,
                limits=limits.interop_limits(),
                source_identity=source_path.name,
            )
        except InteropDependencyError as exc:
            kernel_missing = True
            conversion_error = _failure(exc)
        except InteropError as exc:
            conversion_error = _failure(exc)
        convert_seconds = time.perf_counter() - started
    return ParasolidSource(
        path=source_path,
        digest=digest,
        byte_length=byte_length,
        header=header,
        parsed=parsed,
        parse_error=parse_error,
        conversion=conversion,
        conversion_error=conversion_error,
        kernel_missing=kernel_missing,
        read_seconds=read_seconds,
        convert_seconds=convert_seconds,
        transmit_key=transmit_key,
    )


# -- attributes ---------------------------------------------------------------------


def attribute_colour(attributes: list[Any]) -> Rgba | None:
    """The first ``SDL/TYSA_COLOUR*`` attribute holding three reals in 0..1."""
    for item in attributes:
        if not item.identifier.startswith(_COLOUR_PREFIX):
            continue
        for item_field in item.fields:
            if item_field.kind != "reals" or len(item_field.values) != 3:
                continue
            values = item_field.values
            if all(
                isinstance(v, float) and isfinite(v) and 0.0 <= v <= 1.0 for v in values
            ):
                return (float(values[0]), float(values[1]), float(values[2]), 1.0)
    return None


def attribute_name(attributes: list[Any]) -> str | None:
    """``SDL/TYSA_NAME``, else ``SDL/TYSA_UNAME``, when either holds text."""
    for identifier in _NAME_IDENTIFIERS:
        for item in attributes:
            if item.identifier != identifier:
                continue
            for item_field in item.fields:
                value = item_field.value
                if item_field.kind in {"text", "unicode"} and value and value.strip():
                    return value.strip()
    return None


def _is_mapped_identifier(identifier: str) -> bool:
    return identifier.startswith(_COLOUR_PREFIX) or identifier in _NAME_IDENTIFIERS


# -- classification -----------------------------------------------------------------


@dataclass(slots=True)
class BodyRecord:
    """One Parasolid body with its converted shape, colours and outcome."""

    id: int
    kind: str
    name: str | None
    colour: Rgba | None
    shape: Any | None
    status: str
    reason: str | None
    face_count: int
    faces_dropped: int
    face_colours: dict[int, Rgba]
    face_shapes: list[tuple[Any, Rgba]]
    bounds: Bounds | None
    referenced: bool

    @property
    def key(self) -> str:
        return f"body-{self.id}"

    @property
    def label(self) -> str:
        return self.name or f"body {self.id}"


@dataclass(slots=True)
class Classification:
    """What each body contributes, and which attributes were used."""

    bodies: list[BodyRecord] = field(default_factory=list)
    unsupported_reasons: Counter[str] = field(default_factory=Counter)
    attribute_identifiers: Counter[str] = field(default_factory=Counter)
    unmapped_identifiers: Counter[str] = field(default_factory=Counter)
    coloured_bodies: int = 0
    coloured_faces: int = 0
    named_bodies: int = 0

    @property
    def converted(self) -> list[BodyRecord]:
        return [body for body in self.bodies if body.shape is not None]

    @property
    def by_id(self) -> dict[int, BodyRecord]:
        return {body.id: body for body in self.bodies}


def _short(reason: str) -> str:
    text = " ".join(str(reason).split())
    return text if len(text) <= 160 else text[:157] + "..."


def classify(source: ParasolidSource) -> Classification:
    """Pair every body with its converted shape, colours and name."""
    result = Classification()
    if source.parsed is None:
        return result
    model = source.parsed.brep
    by_owner: dict[tuple[str, int | None], list[Any]] = {}
    for item in model.attributes:
        result.attribute_identifiers[item.identifier] += 1
        if not _is_mapped_identifier(item.identifier):
            result.unmapped_identifiers[item.identifier] += 1
        by_owner.setdefault((item.owner.kind.value, item.owner.id), []).append(item)

    region_body = {region.id: region.body for region in model.regions}
    face_body: dict[int, int] = {}
    for shell in model.shells:
        body_id = region_body.get(shell.region)
        if body_id is None:
            continue
        for face_id in (*shell.front_faces, *shell.back_faces):
            face_body[face_id] = body_id
    faces_of: dict[int, list[Any]] = {}
    for face in model.faces:
        body_id = face_body.get(face.id)
        if body_id is not None:
            faces_of.setdefault(body_id, []).append(face)
    referenced = {
        instance.part
        for instance in model.instances
        if instance.part_kind is InstancePartKind.BODY
    }

    conversion = source.conversion
    shapes_by_key: dict[str, Any] = {}
    targets: dict[str, list[str]] = {}
    failed: dict[int, str] = {}
    partial_ids: set[int] = set()
    dropped: Counter[int] = Counter()
    if conversion is not None:
        shapes_by_key = {item.key: item.shape for item in conversion.subshapes}
        for relation in conversion.source_map.relations:
            targets.setdefault(relation.source.key, []).append(relation.target_key)
        report = conversion.report
        failed = {item.body_id: _short(item.reason) for item in report.bodies_failed}
        partial_ids = set(report.bodies_partial)
        for item in report.faces_dropped:
            if item.body_id is not None:
                dropped[item.body_id] += 1
            result.unsupported_reasons[_short(item.reason)] += 1
        for reason in failed.values():
            result.unsupported_reasons[reason] += 1

    for body in model.bodies:
        attributes = by_owner.get(("body", body.id), [])
        colour = attribute_colour(attributes)
        name = attribute_name(attributes)
        shape = None
        reason: str | None = None
        if conversion is None:
            status = "unconverted"
            if source.conversion_error is not None:
                reason = _short(source.conversion_error.message)
        elif body.id in failed:
            status = "failed"
            reason = failed[body.id]
        else:
            keys = targets.get(f"parasolid:body:{body.id:06d}", [])
            shape = next((shapes_by_key[k] for k in keys if k in shapes_by_key), None)
            if shape is None:
                status = "failed"
                reason = "no OCCT shape was produced for the body"
                result.unsupported_reasons[reason] += 1
            else:
                status = "partial" if body.id in partial_ids else "complete"
        face_colours: dict[int, Rgba] = {}
        face_shapes: list[tuple[Any, Rgba]] = []
        body_faces = faces_of.get(body.id, [])
        if shape is not None:
            coloured: list[tuple[Any, Rgba]] = []
            for face in body_faces:
                face_colour = attribute_colour(by_owner.get(("face", face.id), []))
                if face_colour is None or face_colour == colour:
                    continue
                for key in targets.get(f"parasolid:face:{face.id:06d}", []):
                    if key.startswith("occt:face:") and key in shapes_by_key:
                        coloured.append((shapes_by_key[key], face_colour))
            if coloured:
                pick_ids = face_pick_ids(shape, [item[0] for item in coloured])
                for (occt_face, face_colour), pick_id in zip(
                    coloured, pick_ids, strict=True
                ):
                    if pick_id > 0:
                        face_colours[pick_id] = face_colour
                        face_shapes.append((occt_face, face_colour))
        if colour is not None:
            result.coloured_bodies += 1
        result.coloured_faces += len(face_colours)
        if name is not None:
            result.named_bodies += 1
        result.bodies.append(
            BodyRecord(
                id=body.id,
                kind=str(body.kind.value),
                name=name,
                colour=colour,
                shape=shape,
                status=status,
                reason=reason,
                face_count=len(body_faces),
                faces_dropped=dropped.get(body.id, 0),
                face_colours=face_colours,
                face_shapes=face_shapes,
                bounds=None if shape is None else shape_bounds(shape),
                referenced=body.id in referenced,
            )
        )
    return result


# -- placement ----------------------------------------------------------------------


def transform_matrix(
    transform: Any | None, *, scale: float = SOURCE_TO_TARGET
) -> Matrix4:
    """The row-major matrix of a stored ``TRANSFORM``: ``(R · x + t) · s``.

    ``scale`` converts the translation from the file's metres to millimetres.
    """
    if transform is None:
        return IDENTITY
    s = float(transform.scale)
    rows = []
    for row, shift in zip(
        transform.rotation,
        (transform.translation.x, transform.translation.y, transform.translation.z),
        strict=True,
    ):
        rows.append(
            (
                float(row[0]) * s,
                float(row[1]) * s,
                float(row[2]) * s,
                float(shift) * s * scale,
            )
        )
    rows.append((0.0, 0.0, 0.0, 1.0))
    return tuple(rows)


def _transform_is_supported(transform: Any | None) -> bool:
    if transform is None:
        return True
    perspective = transform.perspective
    if any(abs(float(v)) > 0.0 for v in (perspective.x, perspective.y, perspective.z)):
        return False
    return not transform.has(transform.GENERAL_AFFINE)


@dataclass(slots=True)
class _Node:
    """One placement of the product tree in depth-first order."""

    index: int
    kind: str
    parent: _Node | None
    matrix: Matrix4
    assembly_id: int | None = None
    instance_id: int | None = None
    body_id: int | None = None
    negative: bool = False
    product_id: str = ""
    occurrence_id: str | None = None
    children: list[_Node] = field(default_factory=list)


@dataclass(slots=True)
class PlacementTree:
    nodes: list[_Node]
    unsupported_transforms: int = 0
    negative_instances: int = 0
    truncated: int = 0


def placement_tree(model: Any, classification: Classification) -> PlacementTree:
    """Root assemblies with their instances, then the bodies no instance places."""
    tree = PlacementTree(nodes=[])
    assemblies = {item.id: item for item in model.assemblies}
    instances = {item.id: item for item in model.instances}

    def add(kind: str, parent: _Node | None, matrix: Matrix4, **values: Any) -> _Node:
        node = _Node(len(tree.nodes), kind, parent, matrix, **values)
        tree.nodes.append(node)
        if parent is not None:
            parent.children.append(node)
        return node

    def visit(assembly: Any, parent: _Node, depth: int) -> None:
        if depth > _MAX_DEPTH:
            tree.truncated += 1
            return
        for instance_id in assembly.instances:
            instance = instances.get(instance_id)
            if instance is None:
                continue
            if not _transform_is_supported(instance.transform):
                tree.unsupported_transforms += 1
                matrix = IDENTITY
            else:
                matrix = transform_matrix(instance.transform)
            if instance.negative:
                tree.negative_instances += 1
            node = add(
                "instance",
                parent,
                matrix,
                instance_id=instance.id,
                negative=bool(instance.negative),
            )
            if instance.part_kind is InstancePartKind.BODY:
                node.body_id = instance.part
            else:
                node.assembly_id = instance.part
                child = assemblies.get(instance.part)
                if child is not None:
                    visit(child, node, depth + 1)

    for assembly in model.assemblies:
        if assembly.referencing_instances:
            continue
        root = add("assembly", None, IDENTITY, assembly_id=assembly.id)
        visit(assembly, root, 1)
    for body in classification.bodies:
        if not body.referenced:
            add("body", None, IDENTITY, body_id=body.id)
    return tree


@dataclass(frozen=True, slots=True)
class Placement:
    """One body placed in the model frame (millimetres)."""

    body: BodyRecord
    matrix: Matrix4
    label: str


def placements(model: Any, classification: Classification) -> list[Placement]:
    """Every converted body at each place the assembly structure puts it."""
    by_id = classification.by_id
    tree = placement_tree(model, classification)
    worlds: list[Matrix4] = []
    result: list[Placement] = []
    for node in tree.nodes:
        parent_world = IDENTITY if node.parent is None else worlds[node.parent.index]
        world = matmul(parent_world, node.matrix)
        worlds.append(world)
        if node.body_id is None:
            continue
        body = by_id.get(node.body_id)
        if body is None or body.shape is None:
            continue
        label = body.label
        if node.instance_id is not None:
            label = f"{label} (instance {node.instance_id})"
        result.append(Placement(body, world, label))
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


class ParasolidImporter:
    """Importer for Parasolid transmit files (X_T, X_B, ``.xmt_txt``, ``.xmt_bin``).

    ``limits`` bounds parasolid-kit's parser and converter; ``partial`` keeps
    the bodies whose faces partly fail to build (the dropped faces are
    reported) instead of failing the whole file. The adapter needs
    ``parasolid-kit[occt]``, which the distribution requires.
    """

    format_name = SOURCE_FORMAT
    suffixes = frozenset({".x_t", ".x_b", ".xmt_txt", ".xmt_bin"})

    def __init__(
        self, *, limits: ParasolidLimits | None = None, partial: bool = True
    ) -> None:
        self.limits = ParasolidLimits() if limits is None else limits
        self.partial = partial

    @property
    def accepted_suffixes(self) -> frozenset[str]:
        return self.suffixes

    def probe(self, path: Path) -> float:
        """Return a bounded confidence from the ``**ABCDEF...`` header line."""
        source_path = Path(path)
        try:
            if not source_path.is_file():
                return 0.0
            with source_path.open("rb") as stream:
                head = stream.read(len(SIGNATURE))
        except OSError:
            return 0.0
        if not is_parasolid_signature(head):
            return 0.0
        return 0.9 if source_path.suffix.lower() in self.suffixes else 0.6

    def import_file(
        self,
        path: Path,
        *,
        options: ImportOptions,
        resources: ResourceWriter,
    ) -> ImportResult:
        """Map one transmit file to a package with its bodies and assembly structure."""
        if options.ir_version != CURRENT_IR_VERSION:
            raise UnsupportedIRVersionError(
                f"{ADAPTER_NAME} supports IR {CURRENT_IR_VERSION}, "
                f"not {options.ir_version}"
            )
        source = read_source(path, limits=self.limits, partial=self.partial)
        return map_source(source, options=options, resources=resources)


def map_source(
    source: ParasolidSource,
    *,
    options: ImportOptions,
    resources: ResourceWriter,
) -> ImportResult:
    """Map an already read :class:`ParasolidSource` to an :class:`ImportResult`."""
    key = source.digest[:12]
    stem = source.path.stem or "model"
    name_slug = slug(stem)
    source_id = f"source-{SOURCE_FORMAT}-{key}"
    source_ref = SourceRef(
        document_id=source_id,
        source_kind="parasolid-transmit-file",
        source_id=source.schema_key,
    )
    diagnostics: list[ImportDiagnostic] = []
    classification = classify(source)
    model = None if source.parsed is None else source.parsed.brep
    writer = {
        "application": source.application,
        "parasolid_version": source.parasolid_version,
        "schema_key": source.schema_key,
    }

    # Parse and conversion state ------------------------------------------------
    if source.parse_error is not None:
        failure = source.parse_error
        if failure.schema:
            diagnostics.append(
                _diagnostic(
                    "PARASOLID_SCHEMA_UNSUPPORTED",
                    "error",
                    "unresolved",
                    (
                        f"The transmit file's schema key {source.schema_key or '?'} "
                        f"(written by {source.application or 'unknown application'}, "
                        f"{source.parasolid_version or 'unknown Parasolid'}) is "
                        "not covered by parasolid-kit's built-in profiles: "
                        f"{failure.message}"
                    ),
                    source_id,
                    {**writer, **failure.as_dict()},
                )
            )
        elif failure.limit:
            diagnostics.append(
                _diagnostic(
                    "PARASOLID_LIMIT",
                    "error",
                    "unresolved",
                    f"parasolid-kit stopped at a resource limit: {failure.message}",
                    source_id,
                    {**writer, **failure.as_dict()},
                )
            )
        else:
            diagnostics.append(
                _diagnostic(
                    "PARASOLID_PARSE_FAILED",
                    "error",
                    "unresolved",
                    "parasolid-kit could not read the transmit file: "
                    f"{failure.message}",
                    source_id,
                    {**writer, **failure.as_dict()},
                )
            )
    if source.family_match:
        diagnostics.append(
            _diagnostic(
                "PARASOLID_SCHEMA_FAMILY_MATCH",
                "info",
                "preserved",
                (
                    f"Parasolid build {source.modeller_version or '?'} has no reviewed "
                    f"profile of its own; the file was read with the schema family "
                    f"profile {source.schema_profile}."
                ),
                source_id,
                {**writer, "profile": source.schema_profile},
            )
        )
    if model is not None and not model.complete:
        codes = Counter(item.code for item in model.diagnostics)
        diagnostics.append(
            _diagnostic(
                "PARASOLID_GEOMETRY_UNSUPPORTED",
                "warning",
                "skipped",
                (
                    f"{sum(codes.values())} geometry records are not decoded by "
                    "parasolid-kit; the faces on them are left out."
                ),
                source_id,
                {"records": dict(codes.most_common(12))},
            )
        )
    if source.kernel_missing:
        diagnostics.append(
            _diagnostic(
                "PARASOLID_KERNEL_MISSING",
                "error",
                "unresolved",
                (
                    "The geometry kernel is not installed (parasolid-kit's occt "
                    "extra); the bodies cannot be converted."
                ),
                source_id,
            )
        )
    elif source.conversion_error is not None:
        failure = source.conversion_error
        diagnostics.append(
            _diagnostic(
                "PARASOLID_LIMIT" if failure.limit else "PARASOLID_CONVERSION_FAILED",
                "error",
                "unresolved",
                f"The bodies could not be converted to OCCT: {failure.message}",
                source_id,
                {**writer, **failure.as_dict()},
            )
        )

    # Geometry ------------------------------------------------------------------------
    converted = classification.converted
    tree = (
        placement_tree(model, classification)
        if model is not None
        else PlacementTree(nodes=[])
    )
    by_id = classification.by_id
    world_bounds: Bounds | None = None
    worlds: list[Matrix4] = []
    for node in tree.nodes:
        parent_world = IDENTITY if node.parent is None else worlds[node.parent.index]
        world = matmul(parent_world, node.matrix)
        worlds.append(world)
        body = None if node.body_id is None else by_id.get(node.body_id)
        if body is not None and body.bounds is not None:
            world_bounds = merge_bounds(
                world_bounds, transform_bounds(body.bounds, world)
            )
    linear_deflection = max(
        diagonal(world_bounds) * options.mesh_linear_deflection_ratio, 1e-6
    )
    shapes_by_key = {body.key: body.shape for body in converted}

    # Identifiers -----------------------------------------------------------------
    root_product_id = f"product-{name_slug}-{key}"
    root_configuration_id = f"configuration-{name_slug}-{key}"
    assembly_ids = sorted(
        {node.assembly_id for node in tree.nodes if node.assembly_id is not None}
    )
    product_of_assembly = {
        assembly_id: f"product-assembly-{assembly_id}-{key}"
        for assembly_id in assembly_ids
    }
    configuration_of_assembly = {
        assembly_id: f"configuration-assembly-{assembly_id}-{key}"
        for assembly_id in assembly_ids
    }
    referenced_bodies = sorted(
        {
            node.body_id
            for node in tree.nodes
            if node.kind == "instance" and node.body_id is not None
        }
    )
    product_of_body = {
        body_id: f"product-body-{body_id}-{key}" for body_id in referenced_bodies
    }
    configuration_of_body = {
        body_id: f"configuration-body-{body_id}-{key}" for body_id in referenced_bodies
    }
    ir_body_id = {body.id: f"body-{body.id}-{key}" for body in converted}
    for node in tree.nodes:
        if node.kind == "assembly":
            node.product_id = product_of_assembly[node.assembly_id]  # type: ignore[index]
            node.occurrence_id = f"occurrence-assembly-{node.assembly_id}-{key}"
        elif node.kind == "instance":
            node.product_id = (
                product_of_body[node.body_id]  # type: ignore[index]
                if node.body_id is not None
                else product_of_assembly[node.assembly_id]  # type: ignore[index]
            )
            node.occurrence_id = f"occurrence-instance-{node.instance_id}-{key}"
        else:
            node.product_id = root_product_id

    def node_name(node: _Node) -> str:
        if node.body_id is not None:
            body = by_id.get(node.body_id)
            return body.label if body is not None else f"body {node.body_id}"
        return f"assembly {node.assembly_id}"

    def node_extras(node: _Node) -> dict[str, Any]:
        body_ir_id = None if node.body_id is None else ir_body_id.get(node.body_id)
        extras: dict[str, Any] = {
            "id": node.occurrence_id or body_ir_id or node.product_id,
            "product": node.product_id,
        }
        if body_ir_id is not None:
            extras["body"] = body_ir_id
        if node.negative:
            extras["negative"] = True
        return extras

    glb_nodes = [
        GlbNode(
            name=node_name(node),
            parent=None if node.parent is None else node.parent.index,
            matrix=node.matrix,
            key=(
                by_id[node.body_id].key
                if node.body_id is not None and node.body_id in ir_body_id
                else None
            ),
            extras=node_extras(node),
        )
        for node in tree.nodes
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
                "cad3d_ir_parasolid": {
                    "source_format": SOURCE_FORMAT,
                    "parasolid_kit_version": parasolid_kit_version(),
                    "schema_key": source.schema_key,
                    "application": source.application,
                    "linear_deflection_mm": linear_deflection,
                    "angular_deflection_rad": options.mesh_angular_deflection,
                }
            },
            body_colours={
                body.key: body.colour for body in converted if body.colour is not None
            },
            face_colours={
                body.key: body.face_colours for body in converted if body.face_colours
            },
        )
        if glb_result.failed_keys:
            diagnostics.append(
                _diagnostic(
                    "PARASOLID_MESH_FAILED",
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
            properties={"parasolid_kit_version": parasolid_kit_version()},
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
                        "parasolid-kit B-Rep to OCCT, placed by the assembly "
                        "instances, through OCCT BRepMesh_IncrementalMesh to glTF "
                        "binary"
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
        for index, body in enumerate(converted):
            payload = brep_bytes(body.shape)
            brep_resource = resources.write_chunks(
                id=f"resource-brep-{body.id}-{key}",
                name=f"{body.label} B-Rep",
                uri=f"geometry/{name_slug}-{key}-{index}.brep",
                chunks=_iter_bytes(payload),
                media_type=BREP_MEDIA_TYPE,
                encoding=BREP_ENCODING,
                source=source_ref,
                properties={
                    "parasolid_kit_version": parasolid_kit_version(),
                    "frame": "body",
                },
            )
            resource_records.append(brep_resource)
            representation_id = f"representation-brep-{body.id}-{key}"
            representations.append(
                GeometryRepresentation(
                    id=representation_id,
                    name=f"{body.label} B-Rep",
                    kind="brep",
                    role="design",
                    fidelity="exact" if body.status == "complete" else "approximate",
                    resource_id=brep_resource.id,
                    units=UnitSystem(length_unit="mm"),
                    bounds=(
                        None
                        if body.bounds is None
                        else BBox3(min=body.bounds[0], max=body.bounds[1])
                    ),
                    derivation=RepresentationDerivation(
                        relation="converted",
                        method="parasolid-kit to_occt to OCCT BRepTools ASCII",
                        source=source_ref,
                        metadata={
                            "status": body.status,
                            "faces_dropped": body.faces_dropped,
                        },
                    ),
                    source=source_ref,
                )
            )
            representation_of_body[body.id] = representation_id

    # Package objects ---------------------------------------------------------------
    product_definitions: list[ProductDefinition] = []
    configurations: list[Configuration] = []
    occurrences: list[Occurrence] = []
    bodies: list[Body] = []

    def body_ref(body: BodyRecord) -> SourceRef:
        return SourceRef(
            document_id=source_id,
            source_kind="parasolid-body",
            source_id=str(body.id),
            metadata={"kind": body.kind, "status": body.status},
        )

    for body in converted:
        properties: dict[str, Any] = {
            "parasolid_body_id": body.id,
            "kind": body.kind,
            "status": body.status,
            "faces": body.face_count,
            "faces_dropped": body.faces_dropped,
        }
        if body.colour is not None:
            properties["colour"] = list(body.colour[:3])
        if body.face_colours:
            properties["coloured_faces"] = len(body.face_colours)
        bodies.append(
            Body(
                id=ir_body_id[body.id],
                name=body.label,
                representation_ids=(
                    [representation_of_body[body.id]]
                    if body.id in representation_of_body
                    else []
                ),
                source=body_ref(body),
                properties=properties,
            )
        )

    root_nodes = [node for node in tree.nodes if node.parent is None]
    unreferenced_converted = [
        ir_body_id[node.body_id]
        for node in root_nodes
        if node.kind == "body" and node.body_id in ir_body_id
    ]
    root_occurrences = [
        node.occurrence_id
        for node in root_nodes
        if node.kind == "assembly" and node.occurrence_id is not None
    ]
    product_definitions.append(
        ProductDefinition(
            id=root_product_id,
            name=stem,
            kind="assembly" if root_occurrences else "part",
            source_document_id=source_id,
            default_configuration_id=root_configuration_id,
            source=source_ref,
            properties={
                "application": source.application,
                "parasolid_version": source.parasolid_version,
                "schema_key": source.schema_key,
            },
        )
    )
    configurations.append(
        Configuration(
            id=root_configuration_id,
            name="Default",
            product_id=root_product_id,
            body_ids=unreferenced_converted,
            occurrence_ids=root_occurrences,
            source=source_ref,
        )
    )
    assemblies = {} if model is None else {item.id: item for item in model.assemblies}
    children_of: dict[int, list[_Node]] = {}
    for node in tree.nodes:
        if node.kind == "instance" and node.parent is not None:
            owner = node.parent.assembly_id
            if owner is not None:
                children_of.setdefault(owner, []).append(node)
    for assembly_id in assembly_ids:
        assembly = assemblies.get(assembly_id)
        assembly_ref = SourceRef(
            document_id=source_id,
            source_kind="parasolid-assembly",
            source_id=str(assembly_id),
            metadata={} if assembly is None else {"kind": str(assembly.kind.value)},
        )
        product_definitions.append(
            ProductDefinition(
                id=product_of_assembly[assembly_id],
                name=f"assembly {assembly_id}",
                kind="assembly",
                source_document_id=source_id,
                default_configuration_id=configuration_of_assembly[assembly_id],
                source=assembly_ref,
            )
        )
        # The first expansion of an assembly defines its occurrences; further
        # placements of the same assembly reuse them.
        seen: set[str] = set()
        occurrence_ids: list[str] = []
        for node in children_of.get(assembly_id, []):
            if node.occurrence_id is None or node.occurrence_id in seen:
                continue
            seen.add(node.occurrence_id)
            occurrence_ids.append(node.occurrence_id)
        configurations.append(
            Configuration(
                id=configuration_of_assembly[assembly_id],
                name="Default",
                product_id=product_of_assembly[assembly_id],
                body_ids=[],
                occurrence_ids=occurrence_ids,
                source=assembly_ref,
            )
        )
    for body_id in referenced_bodies:
        body = by_id[body_id]
        product_definitions.append(
            ProductDefinition(
                id=product_of_body[body_id],
                name=body.label,
                kind="part",
                source_document_id=source_id,
                default_configuration_id=configuration_of_body[body_id],
                source=body_ref(body),
            )
        )
        configurations.append(
            Configuration(
                id=configuration_of_body[body_id],
                name="Default",
                product_id=product_of_body[body_id],
                body_ids=[ir_body_id[body_id]] if body_id in ir_body_id else [],
                occurrence_ids=[],
                source=body_ref(body),
            )
        )
    emitted: set[str] = set()
    for node in tree.nodes:
        if node.occurrence_id is None or node.occurrence_id in emitted:
            continue
        emitted.add(node.occurrence_id)
        configuration_id = (
            configuration_of_body[node.body_id]  # type: ignore[index]
            if node.body_id is not None
            else configuration_of_assembly[node.assembly_id]  # type: ignore[index]
        )
        occurrence_ref = SourceRef(
            document_id=source_id,
            source_kind="parasolid-instance"
            if node.kind == "instance"
            else "parasolid-assembly",
            source_id=str(
                node.instance_id if node.kind == "instance" else node.assembly_id
            ),
        )
        occurrences.append(
            Occurrence(
                id=node.occurrence_id,
                name=node_name(node),
                product_id=node.product_id,
                configuration_id=configuration_id,
                transform=Transform(matrix=node.matrix),  # type: ignore[arg-type]
                source=occurrence_ref,
                properties={"negative": True} if node.negative else {},
            )
        )

    # Remaining diagnostics ---------------------------------------------------------
    if converted:
        diagnostics.append(
            _diagnostic(
                "PARASOLID_UNIT_M",
                "info",
                "preserved",
                "Parasolid stores lengths in metres; the package is in millimetres.",
                source_id,
                {"source_unit": SOURCE_UNIT, "target_unit": TARGET_UNIT},
            )
        )
        diagnostics.append(
            _diagnostic(
                "PARASOLID_EXACT_BREP",
                "info",
                "preserved",
                (
                    "Geometry is the file's own B-Rep converted to OCCT; exact "
                    "surfaces are kept."
                ),
                source_id,
                {
                    "bodies_converted": len(converted),
                    "bodies_partial": sum(b.status == "partial" for b in converted),
                },
            )
        )
    if source.conversion is not None and not source.conversion.report.occt_valid:
        diagnostics.append(
            _diagnostic(
                "PARASOLID_SHAPE_INVALID",
                "warning",
                "approximated",
                (
                    "OCCT's BRepCheck rejected the converted shape; the bodies are "
                    "shown as built."
                ),
                source_id,
                {
                    "codes": sorted(
                        {
                            item.code
                            for item in source.conversion.report.diagnostics
                            if item.code.startswith("occt.")
                        }
                    )[:12]
                },
            )
        )
    if classification.coloured_bodies or classification.coloured_faces:
        diagnostics.append(
            _diagnostic(
                "PARASOLID_COLOURS_FROM_ATTRIBUTES",
                "info",
                "preserved",
                (
                    f"Colours come from the SDL/TYSA_COLOUR attributes of "
                    f"{classification.coloured_bodies} bodies and "
                    f"{classification.coloured_faces} faces."
                ),
                source_id,
                {
                    "bodies": classification.coloured_bodies,
                    "faces": classification.coloured_faces,
                },
            )
        )
    unmapped_total = sum(classification.unmapped_identifiers.values())
    if classification.unmapped_identifiers:
        diagnostics.append(
            _diagnostic(
                "PARASOLID_ATTRIBUTES_NOT_MAPPED",
                "info",
                "skipped",
                (
                    f"{unmapped_total} attributes of "
                    f"{len(classification.unmapped_identifiers)} kinds other than the "
                    "system colour and name are not mapped."
                ),
                source_id,
                {
                    "identifiers": dict(
                        classification.unmapped_identifiers.most_common(20)
                    )
                },
            )
        )
    failed_bodies = [b for b in classification.bodies if b.status == "failed"]
    partial_bodies = [b for b in classification.bodies if b.status == "partial"]
    if failed_bodies and not source.kernel_missing:
        diagnostics.append(
            _diagnostic(
                "PARASOLID_BODY_UNSUPPORTED",
                "warning",
                "skipped",
                (
                    f"{len(failed_bodies)} of {len(classification.bodies)} bodies "
                    "could not be converted and are left out."
                ),
                source_id,
                {
                    "bodies": len(failed_bodies),
                    "reasons": dict(classification.unsupported_reasons.most_common(12)),
                },
            )
        )
    if partial_bodies:
        diagnostics.append(
            _diagnostic(
                "PARASOLID_FACES_DROPPED",
                "warning",
                "approximated",
                (
                    f"{sum(b.faces_dropped for b in partial_bodies)} faces of "
                    f"{len(partial_bodies)} bodies could not be built and were "
                    "dropped; those bodies are shown as open shells."
                ),
                source_id,
                {
                    "bodies": {str(b.id): b.faces_dropped for b in partial_bodies[:50]},
                    "reasons": dict(classification.unsupported_reasons.most_common(12)),
                },
            )
        )
    if model is not None and model.assemblies:
        diagnostics.append(
            _diagnostic(
                "PARASOLID_ASSEMBLY_STRUCTURE",
                "info",
                "preserved",
                (
                    f"{len(model.assemblies)} assemblies place {len(model.instances)} "
                    "instances; the stored transforms become occurrences."
                ),
                source_id,
                {
                    "assemblies": len(model.assemblies),
                    "instances": len(model.instances),
                    "occurrences": len(occurrences),
                },
            )
        )
    if tree.negative_instances:
        diagnostics.append(
            _diagnostic(
                "PARASOLID_NEGATIVE_INSTANCE",
                "warning",
                "approximated",
                (
                    f"{tree.negative_instances} instances are negative (subtracted "
                    "from their assembly); they are placed like positive ones."
                ),
                source_id,
                {"instances": tree.negative_instances},
            )
        )
    if tree.unsupported_transforms:
        diagnostics.append(
            _diagnostic(
                "PARASOLID_TRANSFORM_UNSUPPORTED",
                "warning",
                "approximated",
                (
                    f"{tree.unsupported_transforms} instance transforms are general "
                    "affine or perspective; those instances are placed at the origin "
                    "of their assembly."
                ),
                source_id,
                {"instances": tree.unsupported_transforms},
            )
        )
    if tree.truncated:
        diagnostics.append(
            _diagnostic(
                "PARASOLID_ASSEMBLY_TRUNCATED",
                "warning",
                "skipped",
                f"Assembly nesting deeper than {_MAX_DEPTH} levels was not expanded.",
                source_id,
                {"assemblies": tree.truncated},
            )
        )
    if not converted and source.parse_error is None and not source.kernel_missing:
        diagnostics.append(
            _diagnostic(
                "PARASOLID_NO_GEOMETRY",
                "error",
                "unresolved",
                "No body of the file yielded a displayable shape.",
                source_id,
                {
                    "bodies": len(classification.bodies),
                    "reasons": dict(classification.unsupported_reasons.most_common(12)),
                },
            )
        )
    if options.strict and not converted:
        if source.parse_error is not None:
            reason = (
                "unsupported schema key"
                if source.parse_error.schema
                else "unreadable transmit file"
            )
        elif source.kernel_missing:
            reason = "missing geometry kernel"
        else:
            reason = "no displayable body"
        raise ParasolidReadError(f"{reason} in Parasolid document: {source.path}")

    # Package --------------------------------------------------------------------
    header_properties = {
        name: source.header[name]
        for name in ("APPL", "FRU", "SCH", "DATE")
        if name in source.header
    }
    source_document = SourceDocument(
        id=source_id,
        name=source.path.name,
        format=SOURCE_FORMAT,
        version=source.parasolid_version,
        uri=source.path.name,
        sha256=source.digest,
        properties={
            "encoding": source.encoding,
            "schema_key": source.schema_key,
            "modeller_version": source.modeller_version,
            "schema_profile": source.schema_profile,
            "schema_coverage": source.schema_coverage,
            "application": source.application,
            "parasolid_version": source.parasolid_version,
            "header": header_properties,
        },
    )
    metadata: dict[str, Any] = {
        "adapter": ADAPTER_NAME,
        "adapter_version": ADAPTER_VERSION,
        "parasolid_kit_version": parasolid_kit_version(),
        "source_format": SOURCE_FORMAT,
        "encoding": source.encoding,
        "schema_key": source.schema_key,
        "schema_profile": source.schema_profile,
        "schema_coverage": source.schema_coverage,
        "application": source.application,
        "parasolid_version": source.parasolid_version,
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
        "encoding": source.encoding,
        "schema_key": source.schema_key,
        "schema_profile": source.schema_profile,
        "schema_coverage": source.schema_coverage,
        "family_match": source.family_match,
        "application": source.application,
        "parasolid_version": source.parasolid_version,
        "parse_failed": source.parse_error is not None,
        "schema_unsupported": (
            source.parse_error is not None and source.parse_error.schema
        ),
        "source_complete": None if model is None else bool(model.complete),
        "bodies": len(classification.bodies),
        "bodies_converted": len(converted),
        "bodies_partial": len(partial_bodies),
        "bodies_failed": len(failed_bodies),
        "bodies_by_kind": dict(Counter(b.kind for b in classification.bodies)),
        "faces": sum(b.face_count for b in classification.bodies),
        "faces_dropped": sum(b.faces_dropped for b in classification.bodies),
        "unsupported_reasons": dict(classification.unsupported_reasons),
        "assemblies": 0 if model is None else len(model.assemblies),
        "instances": 0 if model is None else len(model.instances),
        "negative_instances": tree.negative_instances,
        "products": len(product_definitions),
        "occurrences": len(occurrences),
        "coloured_bodies": classification.coloured_bodies,
        "coloured_faces": classification.coloured_faces,
        "named_bodies": classification.named_bodies,
        "attribute_identifiers": dict(classification.attribute_identifiers),
        "unique_meshes": len(shapes_by_key),
        "triangles": 0 if glb_result is None else glb_result.triangle_count,
        "line_segments": 0 if glb_result is None else glb_result.line_segment_count,
        "placed_triangles": 0 if glb_result is None else glb_result.placed_triangles,
        "vertices": 0 if glb_result is None else glb_result.vertex_count,
        "nodes": 0 if glb_result is None else glb_result.node_count,
        "materials": 0 if glb_result is None else glb_result.material_count,
        "visualization_byte_length": 0
        if glb_result is None
        else len(glb_result.payload),
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
    "ParasolidImporter",
    "ParasolidLimits",
    "ParasolidSource",
    "Placement",
    "attribute_colour",
    "attribute_name",
    "classify",
    "common_header",
    "is_parasolid_signature",
    "map_source",
    "parasolid_kit_version",
    "placements",
    "read_source",
    "transform_matrix",
    "transmit_encoding",
]
