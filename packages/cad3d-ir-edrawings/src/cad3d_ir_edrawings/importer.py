"""eDrawings parts and assemblies to cad3d-ir through edrawings-kit display bundles.

An ``.eprt``/``.easm`` file is a HOOPS Stream File (HSF), on its own or inside
a ZIP container (sometimes behind a short preamble). ``edrawings-kit`` reads it
with bounded resources and returns a *display bundle*: every body once as
packed buffers in the model's own units, every placement of a body with its
estimated matrix, name and colour, the saved configuration that is shown, and
the preview image. The adapter turns one bundle into one package: the bodies
placed together (same component name, path and matrix) form a product, a
product's placements become occurrences, and one GLB in millimetres shares the
vertex data between the placements of the same product.

Placements are estimates (``EDRAWINGS_PLACEMENT_ESTIMATED``): they follow the
stored segment tree, modelling matrices, includes and repeat instances, which
eDrawings' own arrangement is not guaranteed to match. Coordinates keep the
model's axes; the ``model_units`` option (millimetres per unit) scales them,
and SOLIDWORKS-written files, which omit it, are read as metres.
"""

from __future__ import annotations

import dataclasses
import re
import struct
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import edrawings_kit as ed
from cad3d_ir import (
    Appearance,
    BBox3,
    Body,
    CadPackage,
    Configuration,
    CoordinateSystem,
    ImportDiagnostic,
    ImportResult,
    Occurrence,
    ProductDefinition,
    RepresentationDerivation,
    RootSelection,
    SourceDocument,
    SourceRef,
    Transform,
    UnitSystem,
    Visualization,
    validate_package,
)
from cad3d_ir.constants import CURRENT_IR_VERSION
from cad3d_ir.importers import ImportOptions
from cad3d_ir.io import ResourceWriter
from cad3d_ir.mesh import GLB_ENCODING, GLB_MEDIA_TYPE

from cad3d_ir_edrawings.display import (
    ColorRGBA,
    DisplayGlb,
    GlbPlacement,
    build_display_glb,
    color_key,
    color_name,
    instance_matrix,
)
from cad3d_ir_edrawings.errors import (
    EdrawingsLimitError,
    EdrawingsReadError,
    UnknownSelectionError,
    UnsupportedDocumentKindError,
    UnsupportedIRVersionError,
)
from cad3d_ir_edrawings.preview import PreviewImage, preview_image

ADAPTER_NAME = "cad3d-ir-edrawings"
ADAPTER_VERSION = "0.3.0"
DEFAULT_SCALE_MM_PER_UNIT = 1000.0
"""SOLIDWORKS writers store metres and omit ``model_units``."""

HSF_SIGNATURE = b";; HSF V"
ZIP_SIGNATURE = b"PK\x03\x04"
ARCHIVE_MARKER = b"2008+ archive"
MODEL_ENTRY = b"eModel"
PROBE_BYTES = 1024
LIMIT_PROFILES = ("service", "desktop")
SERVICE_LIMIT_OVERRIDES: dict[str, int] = {
    # Keep the kit's traversal, vertex and instance caps of the service profile
    # but accept the container sizes production assemblies reach: a 2 MB
    # materials.xml holds more than the kit's 20,000 elements.
    "max_file_bytes": 256 << 20,
    "max_entry_bytes": 256 << 20,
    "max_expanded_bytes": 512 << 20,
    "max_output_bytes": 3072 << 20,
    "max_xml_bytes": 64 << 20,
    "max_xml_elements": 2_000_000,
    "max_hsf_vertices": 10_000_000,
    "max_hsf_face_values": 50_000_000,
}

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_INSTANCE_RE = re.compile(r"^(?P<stem>.+?)-(?P<number>[0-9]+)$")
_GENERIC_NAME_RE = re.compile(r"^(?:body|instance|shell|solid)[ _-]?[0-9]*$", re.I)
_NONE_VALUES = frozenset({"", "<none>"})
_SELECTION_ERRORS = frozenset(
    {"display.configuration_unknown", "display.display_state_unknown"}
)
_STOP_CODES = ("edrawings.display_stopped", "edrawings.display_syntax_stopped")
_MAPPED_KIT_CODES = frozenset(
    {
        "edrawings.display_colors",
        "edrawings.display_configuration_selected",
        "edrawings.display_hidden_components",
        "edrawings.display_line_only_skipped",
        "edrawings.display_placement_estimated",
        "edrawings.display_unplaced_bodies",
        "edrawings.materials_unreadable",
        "edrawings.preview_unreadable",
        "hsf.placement_estimated",
        "hsf.rendering_semantics",
        "hsf.traversal_subtree",
        *_STOP_CODES,
    }
)


def edrawings_kit_version() -> str:
    try:
        from importlib.metadata import version

        return version("edrawings-kit")
    except Exception:  # pragma: no cover - installed as a dependency
        return str(getattr(ed, "__version__", "unknown"))


def hsf_core_version() -> str | None:
    """The hsf-core release edrawings-kit was built against, if it reports one."""
    try:
        info = ed.build_info().to_dict()
    except Exception:  # pragma: no cover - defensive
        return None
    value = info.get("hsf_version")
    return str(value) if value else None


def limits_for(profile: str) -> ed.Limits:
    """edrawings-kit limits for a named profile (``service`` or ``desktop``)."""
    if profile == "service":
        return dataclasses.replace(ed.Limits.service(), **SERVICE_LIMIT_OVERRIDES)
    if profile == "desktop":
        return ed.Limits.desktop()
    raise ValueError(f"unknown limit profile {profile!r}; use one of {LIMIT_PROFILES}")


def slug(value: str) -> str:
    text = _SLUG_RE.sub("-", value.casefold()).strip("-")
    return text[:48] or "model"


def _iter_bytes(payload: bytes, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
    for offset in range(0, len(payload), chunk_size):
        yield payload[offset : offset + chunk_size]


def _text(value: Any) -> str | None:
    if not isinstance(value, str) or value in _NONE_VALUES:
        return None
    return value


def _error_code(exc: BaseException) -> str | None:
    diagnostic = getattr(exc, "diagnostic", None)
    if diagnostic is None and exc.args:
        diagnostic = exc.args[0]
    code = getattr(diagnostic, "code", None)
    if isinstance(code, str):
        return code
    head, _, _ = str(exc).partition(":")
    if head and "." in head and " " not in head:
        return head
    return None


# -- signatures ---------------------------------------------------------------


def _first_entry_name(head: bytes, at: int) -> bytes | None:
    if len(head) < at + 30:
        return None
    (length,) = struct.unpack_from("<H", head, at + 26)
    if len(head) < at + 30 + length:
        return None
    return head[at + 30 : at + 30 + length]


def sniff(head: bytes) -> tuple[str | None, bool]:
    """Classify the first bytes: ``(input kind, carries an eDrawings marker)``.

    Kinds follow edrawings-kit: ``raw_hsf`` (``;; HSF V``), ``zip`` (a local
    file header at offset 0) and ``prefixed_zip`` (a short preamble, 92 bytes
    in observed files, before the ZIP). The marker is the ``eModel`` entry
    eDrawings writes first, or the ``2008+ archive`` text of the preamble; a
    raw HSF stream carries no eDrawings-specific marker.
    """
    if head.startswith(HSF_SIGNATURE):
        return "raw_hsf", False
    if head.startswith(ZIP_SIGNATURE):
        return "zip", _first_entry_name(head, 0) == MODEL_ENTRY
    at = head.find(ZIP_SIGNATURE, 16, 512)
    if at >= 0:
        marked = (
            _first_entry_name(head, at) == MODEL_ENTRY or ARCHIVE_MARKER in head[:at]
        )
        return "prefixed_zip", marked
    return None, False


# -- source -------------------------------------------------------------------


@dataclass(slots=True)
class EdrawingsSource:
    """Everything edrawings-kit read from one document before the mapping."""

    path: Path
    digest: str
    byte_length: int
    input_kind: str
    hsf_version: str | None
    document: Any
    display: Any | None
    display_error: tuple[str, str] | None = None
    preview: PreviewImage | None = None
    diagnostics: list[ImportDiagnostic] = field(default_factory=list)
    limit_profile: str = "service"
    entries: list[dict[str, Any]] = field(default_factory=list)

    @property
    def summary(self) -> Any | None:
        return None if self.display is None else self.display.summary

    @property
    def has_geometry(self) -> bool:
        summary = self.summary
        return (
            summary is not None
            and summary.instance_count > 0
            and summary.statistics.triangles > 0
        )

    @property
    def model_units(self) -> float | None:
        summary = self.summary
        if summary is None:
            return None
        units = summary.document.model_units
        return float(units) if units is not None and units > 0 else None

    @property
    def scale(self) -> float:
        """Millimetres per source unit: ``model_units`` or the metre default."""
        units = self.model_units
        return units if units is not None else DEFAULT_SCALE_MM_PER_UNIT

    @property
    def source_id(self) -> str:
        return f"source-edrawings-{self.digest[:12]}"


def read_source(
    path: Path,
    *,
    limit_profile: str = "service",
    limits: ed.Limits | None = None,
    configuration: str | None = None,
    display_state: str | None = None,
    include_hidden: bool = False,
    include_preview: bool = True,
) -> EdrawingsSource:
    """Read the container and its display bundle with edrawings-kit."""
    source_path = Path(path)
    kit_limits = limits if limits is not None else limits_for(limit_profile)
    try:
        document = ed.read_file(source_path, limits=kit_limits)
    except ed.LimitExceededError as exc:
        raise EdrawingsLimitError(
            f"edrawings-kit limit reached while reading {source_path}: {exc}"
        ) from exc
    except ed.EdrawingsError as exc:
        raise EdrawingsReadError(
            f"not a readable eDrawings document: {source_path} ({exc})"
        ) from exc
    inspection = document.inspection
    if inspection.input_kind == "unknown":
        raise EdrawingsReadError(
            f"not an eDrawings container or HSF stream: {source_path}"
        )
    source = EdrawingsSource(
        path=source_path,
        digest=str(inspection.file_sha256),
        byte_length=int(inspection.file_bytes),
        input_kind=str(inspection.input_kind),
        hsf_version=inspection.header.version if inspection.header else None,
        document=document,
        display=None,
        limit_profile=limit_profile,
        entries=[
            {
                "id": entry.id,
                "name": entry.name,
                "role": entry.role,
                "bytes": int(entry.bytes),
            }
            for entry in inspection.entries
        ],
    )
    try:
        source.display = document.read_display(
            placement="scene_graph_v1",
            colors="materials_xml",
            configuration=configuration,
            display_state=display_state,
            include_hidden=include_hidden,
        )
    except ed.LimitExceededError as exc:
        raise EdrawingsLimitError(
            f"edrawings-kit limit reached while reading {source_path}: {exc}"
        ) from exc
    except ed.EdrawingsError as exc:
        code = _error_code(exc)
        if code in _SELECTION_ERRORS:
            raise UnknownSelectionError(str(exc)) from exc
        source.display_error = (code or type(exc).__name__, str(exc))
    if source.display is not None:
        version = _text(source.display.summary.document.hsf_version)
        if version is not None:
            source.hsf_version = version
    if include_preview:
        source.preview = _read_preview(source)
    return source


def _read_preview(source: EdrawingsSource) -> PreviewImage | None:
    data: bytes | None = None
    media_type: str | None = None
    if source.display is not None:
        try:
            found = source.display.preview()
        except ed.EdrawingsError as exc:
            found = None
            source.diagnostics.append(_preview_unreadable(source.source_id, f"{exc}"))
        if found is not None:
            data, media_type = bytes(found[0]), str(found[1])
    if data is None:
        for entry in source.entries:
            if entry["role"] != "preview":
                continue
            try:
                data = bytes(source.document.extract(entry["id"]).data)
            except ed.EdrawingsError as exc:
                source.diagnostics.append(
                    _preview_unreadable(source.source_id, f"{exc}")
                )
            break
    if data is None:
        return None
    try:
        return preview_image(data, media_type)
    except ValueError as exc:
        source.diagnostics.append(_preview_unreadable(source.source_id, f"{exc}"))
        return None


def _preview_unreadable(source_id: str, reason: str) -> ImportDiagnostic:
    return ImportDiagnostic(
        code="EDRAWINGS_PREVIEW_UNREADABLE",
        severity="warning",
        action="skipped",
        message=f"The saved preview image could not be read: {reason}",
        source_document_id=source_id,
    )


# -- placements and products -----------------------------------------------------


@dataclass(slots=True)
class _Member:
    instance: int
    body: int
    color: ColorRGBA | None
    visible: bool


@dataclass(slots=True)
class _Placement:
    """Kit instances that share a component name, path and matrix."""

    index: int
    name: str
    path: str
    values: tuple[float, ...]
    members: list[_Member] = field(default_factory=list)

    @property
    def bodies(self) -> tuple[int, ...]:
        return tuple(dict.fromkeys(member.body for member in self.members))

    @property
    def visible(self) -> bool:
        return any(member.visible for member in self.members)

    def color_of(self, body: int) -> ColorRGBA | None:
        for member in self.members:
            if member.body == body:
                return member.color
        return None


@dataclass(slots=True)
class _Group:
    """Placements of the same set of bodies: one product definition."""

    index: int
    bodies: tuple[int, ...]
    placements: list[_Placement] = field(default_factory=list)


def _placements(summary: Any, buffers: Any) -> list[_Placement]:
    count = summary.instance_count
    if count == 0:
        return []
    matrices = struct.unpack(f"<{16 * count}d", buffers.matrices.data)
    bodies = struct.unpack(f"<{count}I", buffers.bodies.data)
    names = struct.unpack(f"<{count}I", buffers.names.data)
    paths = struct.unpack(f"<{count}I", buffers.component_paths.data)
    colors = bytes(buffers.colors.data)
    visibility = bytes(buffers.visibility.data)
    table: dict[tuple[Any, ...], _Placement] = {}
    for j in range(count):
        values = matrices[16 * j : 16 * j + 16]
        key = (names[j], paths[j], values)
        placement = table.get(key)
        if placement is None:
            placement = _Placement(
                index=len(table),
                name=summary.names[names[j]] if names[j] < len(summary.names) else "",
                path=(
                    summary.component_paths[paths[j]]
                    if paths[j] < len(summary.component_paths)
                    else ""
                ),
                values=values,
            )
            table[key] = placement
        placement.members.append(
            _Member(
                instance=j,
                body=int(bodies[j]),
                color=color_key(colors[4 * j : 4 * j + 4]),
                visible=visibility[j] != 0,
            )
        )
    return list(table.values())


def _groups(placements: Sequence[_Placement]) -> list[_Group]:
    table: dict[tuple[int, ...], _Group] = {}
    for placement in placements:
        key = tuple(sorted(placement.bodies))
        group = table.get(key)
        if group is None:
            group = _Group(index=len(table), bodies=key)
            table[key] = group
        group.placements.append(placement)
    return list(table.values())


def product_name(
    names: Sequence[str], fallback: str, *, prefer_fallback_for_generic: bool = False
) -> str:
    """Name a product from its placements: the shared stem of ``<stem>-<n>``
    instance names, else the most common name, else ``fallback``. With
    ``prefer_fallback_for_generic`` generic names such as ``Body 0`` or
    ``instance3`` also yield ``fallback`` (the file name of a single-part file).
    """
    cleaned = [name for name in names if name]
    if not cleaned:
        return fallback
    if prefer_fallback_for_generic and all(
        _GENERIC_NAME_RE.match(name) for name in cleaned
    ):
        return fallback
    if len(cleaned) >= 2:
        matches = [_INSTANCE_RE.match(name) for name in cleaned]
        stems = {match.group("stem") for match in matches if match is not None}
        if len(stems) == 1 and all(match is not None for match in matches):
            return stems.pop()
    return Counter(cleaned).most_common(1)[0][0]


# -- importer -------------------------------------------------------------------


class EdrawingsImporter:
    """Importer for eDrawings parts (``.eprt``) and assemblies (``.easm``).

    ``accept_drawings=True`` also imports ``.edrw`` files, which hold the 2D
    sheets of a drawing; only the 3D bodies they may carry are read and the
    sheets are reported as ignored. ``configuration`` selects a saved
    configuration (``config name`` or ``configN`` segment) and
    ``display_state`` a saved display state of it; by default the file's
    active configuration and its current display state are shown.
    ``include_hidden`` keeps components eDrawings hides as invisible
    occurrences (they stay out of the GLB). ``limit_profile`` picks the
    edrawings-kit resource limits (``service`` is bounded for shared servers,
    ``desktop`` is larger); ``limits`` overrides them with explicit values.
    """

    format_name = "edrawings"
    suffixes = frozenset({".eprt", ".easm"})

    def __init__(
        self,
        *,
        accept_drawings: bool = False,
        include_preview: bool = True,
        configuration: str | None = None,
        display_state: str | None = None,
        include_hidden: bool = False,
        limit_profile: str = "service",
        limits: ed.Limits | None = None,
    ) -> None:
        if limit_profile not in LIMIT_PROFILES:
            raise ValueError(
                f"unknown limit profile {limit_profile!r}; use one of {LIMIT_PROFILES}"
            )
        self.accept_drawings = accept_drawings
        self.include_preview = include_preview
        self.configuration = configuration
        self.display_state = display_state
        self.include_hidden = include_hidden
        self.limit_profile = limit_profile
        self.limits = limits

    @property
    def accepted_suffixes(self) -> frozenset[str]:
        if self.accept_drawings:
            return self.suffixes | {".edrw"}
        return self.suffixes

    def probe(self, path: Path) -> float:
        """Return a bounded confidence from the container signature and suffix."""
        source_path = Path(path)
        try:
            if not source_path.is_file():
                return 0.0
            with source_path.open("rb") as stream:
                head = stream.read(PROBE_BYTES)
        except OSError:
            return 0.0
        kind, marked = sniff(head)
        if kind is None:
            return 0.0
        # HSF is HOOPS' generic stream format; eDrawings is its usual source.
        score = 0.5 if kind == "raw_hsf" else 0.6 if marked else 0.2
        if source_path.suffix.lower() in self.accepted_suffixes:
            score += 0.4
        return min(score, 1.0)

    def import_file(
        self,
        path: Path,
        *,
        options: ImportOptions,
        resources: ResourceWriter,
    ) -> ImportResult:
        """Map one eDrawings document to a package with shared display meshes."""
        source_path = Path(path)
        if options.ir_version != CURRENT_IR_VERSION:
            raise UnsupportedIRVersionError(
                f"{ADAPTER_NAME} supports IR {CURRENT_IR_VERSION}, "
                f"not {options.ir_version}"
            )
        if not source_path.is_file():
            raise EdrawingsReadError(f"eDrawings source is not a file: {source_path}")
        if source_path.suffix.lower() == ".edrw" and not self.accept_drawings:
            raise UnsupportedDocumentKindError(
                "eDrawings drawings (.edrw) hold 2D sheets; pass "
                f"accept_drawings=True to read their 3D bodies: {source_path}"
            )
        source = read_source(
            source_path,
            limit_profile=self.limit_profile,
            limits=self.limits,
            configuration=self.configuration,
            display_state=self.display_state,
            include_hidden=self.include_hidden,
            include_preview=self.include_preview,
        )
        return _map_source(source, options=options, resources=resources)


# -- mapping ------------------------------------------------------------------


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


def _shown_configuration(summary: Any) -> tuple[str | None, str | None]:
    """``(name, segment)`` of the configuration the bundle shows."""
    configurations = summary.configurations
    for candidate in configurations.candidates:
        if candidate.node_id == configurations.selected_node_id:
            return _text(candidate.name), _text(candidate.segment_name)
    return None, None


def _kit_diagnostics(summary: Any) -> tuple[Counter[str], dict[str, Any]]:
    counts: Counter[str] = Counter()
    first: dict[str, Any] = {}
    for item in summary.diagnostics:
        counts[item.code] += 1
        first.setdefault(item.code, item)
    return counts, first


def _map_source(
    source: EdrawingsSource,
    *,
    options: ImportOptions,
    resources: ResourceWriter,
) -> ImportResult:
    key = source.digest[:12]
    stem = source.path.stem or "model"
    name_slug = slug(stem)
    suffix = source.path.suffix.lower()
    source_id = source.source_id
    root_product_id = f"product-{name_slug}-{key}"
    root_configuration_id = f"configuration-{name_slug}-{key}"
    resource_id = f"resource-{name_slug}-{key}"
    visualization_id = f"visualization-{name_slug}-{key}"
    preview_resource_id = f"resource-preview-{name_slug}-{key}"

    summary = source.summary
    statistics_kit = None if summary is None else summary.statistics
    document = None if summary is None else summary.document
    writer = None if document is None else _text(document.writer)
    writer_year = None if document is None else document.writer_year_hint
    mount_path = None if document is None else _text(document.mount_path)
    scale = source.scale
    configuration_name, configuration_segment = (
        (None, None) if summary is None else _shown_configuration(summary)
    )
    if configuration_name is None and document is not None:
        configuration_name = _text(document.configuration_candidate)
    root_configuration_name = configuration_name or configuration_segment or "Default"
    display_state = (
        None if summary is None else _text(summary.configurations.shown_display_state)
    )
    kit_counts, kit_first = (
        (Counter(), {}) if summary is None else _kit_diagnostics(summary)
    )

    source_ref = SourceRef(
        document_id=source_id,
        source_kind="edrawings-kit-display",
        source_path=None if document is None else _text(document.entry_id),
    )
    source_properties: dict[str, Any] = {
        "suffix": suffix,
        "input_kind": source.input_kind,
        "hsf_version": source.hsf_version,
        "writer": writer,
        "writer_year_hint": writer_year,
        "configuration_candidate": (
            None if document is None else _text(document.configuration_candidate)
        ),
        "revision_history": (
            None if document is None else _text(document.revision_history)
        ),
        "mount_path": mount_path,
        "model_name": None if document is None else _text(document.model_name),
        "model_units": source.model_units,
        "entries": source.entries,
        "configurations": (
            []
            if summary is None
            else [
                {
                    "segment": candidate.segment_name,
                    "name": _text(candidate.name),
                    "instances": int(candidate.instances),
                    "hidden": bool(candidate.hidden),
                    "display_state": _text(candidate.display_state),
                }
                for candidate in summary.configurations.candidates
            ]
        ),
    }
    source_document = SourceDocument(
        id=source_id,
        name=source.path.name,
        format="edrawings",
        version=writer,
        uri=source.path.name,
        sha256=source.digest,
        properties=source_properties,
    )

    diagnostics: list[ImportDiagnostic] = list(source.diagnostics)
    if suffix == ".edrw":
        diagnostics.append(
            _diagnostic(
                "EDRAWINGS_DRAWING_SHEETS_IGNORED",
                "warning",
                "skipped",
                "This is an eDrawings drawing; its 2D sheets are not read and only "
                "the 3D bodies stored with it, if any, are imported.",
                source_id,
            )
        )

    placements: list[_Placement] = []
    groups: list[_Group] = []
    if summary is not None and summary.instance_count > 0:
        placements = _placements(summary, source.display.instance_buffers())
        groups = _groups(placements)

    # Products, bodies and occurrences.
    product_definitions: list[ProductDefinition] = []
    configurations: list[Configuration] = []
    occurrences: list[Occurrence] = []
    bodies: list[Body] = []
    appearances: dict[ColorRGBA, Appearance] = {}
    glb_placements: list[GlbPlacement] = []
    body_ids: dict[tuple[int, int], str] = {}
    hidden_placements = 0

    def appearance_for(color: ColorRGBA) -> str:
        appearance = appearances.get(color)
        if appearance is None:
            appearance = Appearance(
                id=f"appearance-{color_name(color)[1:].replace('/', '-')}-{key}",
                name=color_name(color),
                base_color=color,
                source=source_ref,
            )
            appearances[color] = appearance
        return appearance.id

    for group in groups:
        number = group.index + 1
        product_id = f"product-{number}-{key}"
        configuration_id = f"configuration-{number}-{key}"
        names = [placement.name for placement in group.placements]
        fallback = (
            stem
            if len(groups) == 1
            else (
                _text(summary.bodies[group.bodies[0]].name) or f"Part {number}"
                if summary is not None
                else f"Part {number}"
            )
        )
        label = product_name(
            names, fallback, prefer_fallback_for_generic=len(groups) == 1
        )
        group_body_ids: list[str] = []
        for body in group.bodies:
            info = summary.bodies[body]
            body_id = f"body-{number}-{body + 1}-{key}"
            body_ids[(group.index, body)] = body_id
            group_body_ids.append(body_id)
            colors = Counter(
                color
                for placement in group.placements
                if (color := placement.color_of(body)) is not None
            )
            bodies.append(
                Body(
                    id=body_id,
                    name=_text(info.name) or f"Body {body + 1}",
                    appearance_id=(
                        appearance_for(colors.most_common(1)[0][0]) if colors else None
                    ),
                    source=SourceRef(
                        document_id=source_id,
                        source_kind="edrawings-kit-body",
                        source_id=_text(info.mesh_id),
                        source_path=_text(info.source_node_path),
                    ),
                )
            )
        product_definitions.append(
            ProductDefinition(
                id=product_id,
                name=label,
                kind="part",
                source_document_id=source_id,
                default_configuration_id=configuration_id,
                source=source_ref,
            )
        )
        configurations.append(
            Configuration(
                id=configuration_id,
                name="Default",
                product_id=product_id,
                body_ids=group_body_ids,
                source=source_ref,
            )
        )
        for placement in group.placements:
            occurrence_id = f"occurrence-{placement.index + 1}-{key}"
            matrix = instance_matrix(placement.values, scale)
            occurrence_name = placement.name or label
            occurrences.append(
                Occurrence(
                    id=occurrence_id,
                    name=occurrence_name,
                    product_id=product_id,
                    configuration_id=configuration_id,
                    transform=Transform(matrix=matrix),
                    visible=placement.visible,
                    suppressed=False,
                    source=SourceRef(
                        document_id=source_id,
                        source_kind="edrawings-kit-instance",
                        source_path=placement.path or None,
                    ),
                )
            )
            if not placement.visible:
                hidden_placements += 1
                continue
            glb_placements.append(
                GlbPlacement(
                    name=occurrence_name,
                    node_id=occurrence_id,
                    product_id=product_id,
                    matrix=matrix,
                    bodies=placement.bodies,
                    colors=tuple(placement.color_of(body) for body in placement.bodies),
                    body_ids=tuple(
                        body_ids[(group.index, body)] for body in placement.bodies
                    ),
                )
            )

    if suffix == ".easm":
        root_kind = "assembly"
    elif suffix == ".eprt":
        root_kind = "part"
    else:
        root_kind = (
            "assembly"
            if len(groups) > 1 or any(len(group.placements) > 1 for group in groups)
            else "part"
        )
    product_definitions.insert(
        0,
        ProductDefinition(
            id=root_product_id,
            name=stem,
            kind=root_kind,
            source_document_id=source_id,
            default_configuration_id=root_configuration_id,
            source=source_ref,
        ),
    )
    configurations.insert(
        0,
        Configuration(
            id=root_configuration_id,
            name=root_configuration_name,
            product_id=root_product_id,
            occurrence_ids=[occurrence.id for occurrence in occurrences],
            source=source_ref,
        ),
    )

    # Visualization.
    visualizations: list[Visualization] = []
    resource_records: list[Any] = []
    display_glb: DisplayGlb | None = None
    glb_length = 0
    metadata: dict[str, Any] = {
        "adapter": ADAPTER_NAME,
        "adapter_version": ADAPTER_VERSION,
        "edrawings_kit_version": edrawings_kit_version(),
        "hsf_core_version": hsf_core_version(),
        "edrawings": {
            "input_kind": source.input_kind,
            "hsf_version": source.hsf_version,
            "writer": writer,
            "writer_year": writer_year,
            "configuration": configuration_name or configuration_segment,
            "display_state": display_state,
            "mount_path": mount_path,
            "model_units": source.model_units,
            "scale_mm_per_unit": scale,
            "placement": "estimated",
            "display_status": None if summary is None else summary.status,
            "placement_status": (None if summary is None else summary.placement_status),
        },
    }
    if source.has_geometry and glb_placements and options.include_visualization_mesh:
        display_glb = build_display_glb(
            source.display,
            name=stem,
            scale=scale,
            placements=glb_placements,
            include_edges=options.mesh_include_edges,
            generator=f"{ADAPTER_NAME} {ADAPTER_VERSION}",
            root_extras={
                "cad3d_ir_edrawings": {
                    "edrawings_kit_version": edrawings_kit_version(),
                    "hsf_version": source.hsf_version,
                    "writer": writer,
                    "placement": "estimated",
                    "scale_mm_per_unit": scale,
                }
            },
        )
        glb_length = len(display_glb.payload)
        bounds = (
            None
            if display_glb.bounds is None
            else BBox3(min=display_glb.bounds[0], max=display_glb.bounds[1])
        )
        resource = resources.write_chunks(
            id=resource_id,
            name=f"{stem} display mesh",
            uri=f"geometry/{name_slug}-{key}.glb",
            chunks=_iter_bytes(display_glb.payload),
            media_type=GLB_MEDIA_TYPE,
            encoding=GLB_ENCODING,
            source=source_ref,
            properties={
                "edrawings_kit_version": edrawings_kit_version(),
                "scale_mm_per_unit": scale,
            },
        )
        resource_records.append(resource)
        visualizations.append(
            Visualization(
                id=visualization_id,
                name=f"{stem} visualization",
                kind="glb",
                scope="package",
                resource_id=resource.id,
                units=UnitSystem(length_unit="mm"),
                bounds=bounds,
                derivation=RepresentationDerivation(
                    relation="tessellated",
                    method=(
                        "cad3d-ir GlbBuilder from edrawings-kit display bundles "
                        "(saved eDrawings tessellation, estimated placements)"
                    ),
                    source=source_ref,
                    metadata={
                        "include_edges": options.mesh_include_edges,
                        "placement": "estimated",
                        "scale_mm_per_unit": scale,
                    },
                ),
                statistics={
                    "triangles": display_glb.triangle_count,
                    "mesh_triangles": display_glb.mesh_triangle_count,
                    "line_segments": display_glb.line_segment_count,
                    "mesh_line_segments": display_glb.mesh_line_segment_count,
                    "vertices": display_glb.vertex_count,
                    "nodes": display_glb.node_count,
                    "meshes": display_glb.mesh_count,
                    "placements": display_glb.placement_count,
                    "bodies": display_glb.body_count,
                },
                source=source_ref,
            )
        )

    # Diagnostics about the geometry.
    if source.has_geometry:
        diagnostics.append(
            _diagnostic(
                "EDRAWINGS_DISPLAY_MESH_ONLY",
                "info",
                "preserved",
                "Geometry is the tessellation eDrawings saved with the document; "
                "no exact surfaces are available from this path.",
                source_id,
                {
                    "bodies": int(statistics_kit.bodies),
                    "instances": int(statistics_kit.instances),
                    "triangles": int(statistics_kit.triangles),
                },
            )
        )
        estimated = kit_first.get("edrawings.display_placement_estimated")
        if estimated is not None or summary.placement_status != "complete":
            reasons = sorted(
                code
                for code in kit_counts
                if code.startswith("hsf.") and code not in {"hsf.traversal_subtree"}
            )
            diagnostics.append(
                _diagnostic(
                    "EDRAWINGS_PLACEMENT_ESTIMATED",
                    "info",
                    "approximated",
                    (
                        estimated.message
                        if estimated is not None
                        else "Placements follow the stored scene graph; the "
                        "producer's own arrangement is not verified."
                    ),
                    source_id,
                    {
                        "placement_status": summary.placement_status,
                        "instances": int(statistics_kit.instances),
                        "withheld_by": reasons,
                    },
                )
            )
        selected = kit_first.get("edrawings.display_configuration_selected")
        if summary.configurations.candidates:
            diagnostics.append(
                _diagnostic(
                    "EDRAWINGS_CONFIGURATION_SELECTED",
                    "info",
                    "preserved",
                    (
                        selected.message
                        if selected is not None
                        else f"Showing configuration {root_configuration_name}."
                    ),
                    source_id,
                    {
                        "selection": summary.configurations.selection,
                        "configuration": configuration_name or configuration_segment,
                        "candidates": len(summary.configurations.candidates),
                        "display_state": display_state,
                        "display_state_selection": (
                            summary.configurations.display_state_selection
                        ),
                    },
                )
            )
        if statistics_kit.hidden_instances or hidden_placements:
            hidden = kit_first.get("edrawings.display_hidden_components")
            diagnostics.append(
                _diagnostic(
                    "EDRAWINGS_HIDDEN_COMPONENTS_SKIPPED",
                    "info",
                    "skipped",
                    (
                        hidden.message
                        if hidden is not None
                        else f"{hidden_placements} placements are hidden by "
                        "eDrawings and are left out of the visualization."
                    ),
                    source_id,
                    {
                        "hidden_instances": int(statistics_kit.hidden_instances),
                        "hidden_bodies": int(statistics_kit.hidden_bodies),
                        "hidden_occurrences": hidden_placements,
                        "display_state": display_state,
                    },
                )
            )
        if statistics_kit.unplaced_bodies:
            unplaced = kit_first.get("edrawings.display_unplaced_bodies")
            diagnostics.append(
                _diagnostic(
                    "EDRAWINGS_UNPLACED_BODIES",
                    "warning",
                    "skipped",
                    (
                        unplaced.message
                        if unplaced is not None
                        else f"{statistics_kit.unplaced_bodies} bodies have no "
                        "placement in the shown configuration and are not drawn."
                    ),
                    source_id,
                    {"bodies": int(statistics_kit.unplaced_bodies)},
                )
            )
        if statistics_kit.line_only_instances_skipped:
            diagnostics.append(
                _diagnostic(
                    "EDRAWINGS_LINE_ONLY_BODIES_SKIPPED",
                    "info",
                    "skipped",
                    f"{statistics_kit.line_only_instances_skipped} placements "
                    "carry only line geometry and are left out.",
                    source_id,
                    {"instances": int(statistics_kit.line_only_instances_skipped)},
                )
            )
        colored = (
            int(statistics_kit.colors_from_hsf)
            + int(statistics_kit.colors_from_materials)
            + int(statistics_kit.colors_from_options)
        )
        if colored == 0:
            diagnostics.append(
                _diagnostic(
                    "EDRAWINGS_APPEARANCE_NOT_SAVED",
                    "info",
                    "skipped",
                    "No colour is stored for the shown bodies; the default face "
                    "colour is used.",
                    source_id,
                )
            )
        elif statistics_kit.colors_from_materials or statistics_kit.colors_from_options:
            diagnostics.append(
                _diagnostic(
                    "EDRAWINGS_COLOR_FROM_MATERIALS",
                    "info",
                    "approximated",
                    f"{statistics_kit.colors_from_materials} placements take their "
                    "colour from materials.xml and "
                    f"{statistics_kit.colors_from_options} from component options "
                    "rather than from the HSF stream.",
                    source_id,
                    {
                        "from_hsf": int(statistics_kit.colors_from_hsf),
                        "from_materials": int(statistics_kit.colors_from_materials),
                        "from_options": int(statistics_kit.colors_from_options),
                    },
                )
            )
        if "hsf.rendering_semantics" in kit_counts:
            diagnostics.append(
                _diagnostic(
                    "EDRAWINGS_RENDER_SETTINGS_IGNORED",
                    "info",
                    "skipped",
                    "Render settings stored in the stream (lighting, display modes, "
                    "line styles) are not evaluated; geometry and colours are kept.",
                    source_id,
                    {"records": int(kit_counts["hsf.rendering_semantics"])},
                )
            )
    if "edrawings.materials_unreadable" in kit_counts:
        item = kit_first["edrawings.materials_unreadable"]
        diagnostics.append(
            _diagnostic(
                "EDRAWINGS_MATERIALS_UNREADABLE",
                "warning",
                "skipped",
                item.message,
                source_id,
            )
        )
    stopped_record = (
        None if statistics_kit is None else statistics_kit.stopped_record_id
    )
    if stopped_record is not None or any(code in kit_counts for code in _STOP_CODES):
        item = next(
            (kit_first[code] for code in _STOP_CODES if code in kit_first), None
        )
        diagnostics.append(
            _diagnostic(
                "EDRAWINGS_STOPPED_AT",
                "warning",
                "skipped",
                (
                    item.message
                    if item is not None
                    else "The stream could not be read to its end; the bodies read "
                    "before the stop are kept."
                ),
                source_id,
                {
                    "record_id": stopped_record,
                    "kit_code": None if item is None else item.code,
                    "processed_records": (
                        None
                        if statistics_kit is None
                        else int(statistics_kit.processed_records)
                    ),
                    "records": (
                        None if statistics_kit is None else int(statistics_kit.records)
                    ),
                },
            )
        )
    remaining = {
        code: count
        for code, count in sorted(kit_counts.items())
        if code not in _MAPPED_KIT_CODES
    }
    if remaining:
        diagnostics.append(
            _diagnostic(
                "EDRAWINGS_READER_DIAGNOSTICS",
                "info",
                "preserved",
                f"edrawings-kit reported {sum(remaining.values())} further "
                f"diagnostics in {len(remaining)} codes; the stream was read as far "
                "as the counts above say.",
                source_id,
                {"codes": remaining},
            )
        )

    # Nothing to show.
    if source.display_error is not None:
        code, message = source.display_error
        diagnostics.append(
            _diagnostic(
                "EDRAWINGS_VERSION_UNSUPPORTED",
                "error",
                "unresolved",
                f"edrawings-kit could not read the display bundle of this HSF "
                f"{source.hsf_version or 'stream'}: {message}",
                source_id,
                {"kit_code": code, "hsf_version": source.hsf_version},
            )
        )
    if not source.has_geometry:
        if source.preview is not None:
            diagnostics.append(
                _diagnostic(
                    "EDRAWINGS_PREVIEW_ONLY",
                    "warning",
                    "skipped",
                    "No 3D body could be read from this document; only the saved "
                    "preview image and the document metadata are available.",
                    source_id,
                    {
                        "input_kind": source.input_kind,
                        "hsf_version": source.hsf_version,
                        "display_status": None if summary is None else summary.status,
                    },
                )
            )
        else:
            if source.display_error is None:
                diagnostics.append(
                    _diagnostic(
                        "EDRAWINGS_NO_GEOMETRY",
                        "error",
                        "unresolved",
                        "The document carries neither readable 3D bodies nor a "
                        "preview image; only metadata was read.",
                        source_id,
                        {
                            "input_kind": source.input_kind,
                            "hsf_version": source.hsf_version,
                            "display_status": (
                                None if summary is None else summary.status
                            ),
                        },
                    )
                )
            if options.strict:
                raise EdrawingsReadError(
                    f"no 3D bodies in eDrawings document: {source.path}"
                )

    preview_length = 0
    if source.preview is not None:
        preview = source.preview
        preview_record = resources.write_chunks(
            id=preview_resource_id,
            name=f"{stem} preview image",
            uri=f"preview/{name_slug}-{key}.{preview.suffix}",
            chunks=(preview.data,),
            media_type=preview.media_type,
            encoding=preview.encoding,
            source=SourceRef(
                document_id=source_id,
                source_kind="edrawings-preview-image",
                source_id=preview.source_kind,
            ),
            properties={
                "role": "preview-image",
                "width": preview.width,
                "height": preview.height,
            },
        )
        resource_records.append(preview_record)
        preview_length = len(preview.data)
        metadata["preview_resource_id"] = preview_record.id

    package = CadPackage(
        format="cad3d-ir",
        version=CURRENT_IR_VERSION,
        id=f"package-edrawings-{key}",
        name=stem,
        units=UnitSystem(length_unit="mm"),
        coordinate_system=CoordinateSystem(),
        source_documents=[source_document],
        product_definitions=product_definitions,
        configurations=configurations,
        occurrences=occurrences,
        bodies=bodies,
        resources=resource_records,
        appearances=list(appearances.values()),
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
        "input_kind": source.input_kind,
        "hsf_version": source.hsf_version,
        "writer": writer,
        "writer_year": writer_year,
        "model_units": source.model_units,
        "scale_mm_per_unit": scale,
        "display_status": None if summary is None else summary.status,
        "placement_status": None if summary is None else summary.placement_status,
        "records": 0 if statistics_kit is None else int(statistics_kit.records),
        "processed_records": (
            0 if statistics_kit is None else int(statistics_kit.processed_records)
        ),
        "stopped": stopped_record is not None,
        "stopped_record_id": stopped_record,
        "bodies": 0 if statistics_kit is None else int(statistics_kit.bodies),
        "instances": 0 if statistics_kit is None else int(statistics_kit.instances),
        "hidden_instances": (
            0 if statistics_kit is None else int(statistics_kit.hidden_instances)
        ),
        "hidden_bodies": (
            0 if statistics_kit is None else int(statistics_kit.hidden_bodies)
        ),
        "unplaced_bodies": (
            0 if statistics_kit is None else int(statistics_kit.unplaced_bodies)
        ),
        "line_only_instances_skipped": (
            0
            if statistics_kit is None
            else int(statistics_kit.line_only_instances_skipped)
        ),
        "colors_from_hsf": (
            0 if statistics_kit is None else int(statistics_kit.colors_from_hsf)
        ),
        "colors_from_materials": (
            0 if statistics_kit is None else int(statistics_kit.colors_from_materials)
        ),
        "colors_from_options": (
            0 if statistics_kit is None else int(statistics_kit.colors_from_options)
        ),
        "configurations": (
            0 if summary is None else len(summary.configurations.candidates)
        ),
        "configuration": configuration_name or configuration_segment,
        "display_state": display_state,
        "products": len(product_definitions),
        "parts": len(groups),
        "occurrences": len(occurrences),
        "hidden_occurrences": hidden_placements,
        "package_bodies": len(bodies),
        "appearances": len(appearances),
        "triangles": 0 if display_glb is None else display_glb.triangle_count,
        "mesh_triangles": 0 if display_glb is None else display_glb.mesh_triangle_count,
        "line_segments": 0 if display_glb is None else display_glb.line_segment_count,
        "vertices": 0 if display_glb is None else display_glb.vertex_count,
        "nodes": 0 if display_glb is None else display_glb.node_count,
        "meshes": 0 if display_glb is None else display_glb.mesh_count,
        "uncolored_bodies": 0 if display_glb is None else display_glb.uncolored,
        "resources": len(resource_records),
        "visualization_byte_length": glb_length,
        "preview_byte_length": preview_length,
    }
    return ImportResult(package=package, diagnostics=diagnostics, statistics=statistics)


__all__ = [
    "ADAPTER_NAME",
    "ADAPTER_VERSION",
    "DEFAULT_SCALE_MM_PER_UNIT",
    "LIMIT_PROFILES",
    "SERVICE_LIMIT_OVERRIDES",
    "EdrawingsImporter",
    "EdrawingsSource",
    "edrawings_kit_version",
    "hsf_core_version",
    "limits_for",
    "product_name",
    "read_source",
    "sniff",
]
