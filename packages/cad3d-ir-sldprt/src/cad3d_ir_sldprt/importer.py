"""SOLIDWORKS part documents to cad3d-ir through sldkit's saved display meshes.

The adapter does not read the Parasolid B-Rep. It takes the triangles that
SOLIDWORKS saved for its own display (``DisplayLists``), which sldkit returns
as packed buffers, and writes them as one GLB. Document metadata (internal
version, configuration names, custom properties, the saved preview image)
comes from sldkit's bounded metadata parse. Documents without display data,
typically parts saved before SOLIDWORKS 2015 in the older ``__Zip`` encoding,
still yield a package with metadata and, when present, the preview image.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from hashlib import sha256
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import sldkit
from cad3d_ir import (
    Appearance,
    BBox3,
    Body,
    CadPackage,
    Configuration,
    CoordinateSystem,
    GeometryRepresentation,
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

from cad3d_ir_sldprt.assembly import (
    AssemblyLayout,
    PlacedInstance,
    instance_colors,
    resolve_layout,
)
from cad3d_ir_sldprt.display import DisplayGlb, build_assembly_glb, build_display_glb
from cad3d_ir_sldprt.errors import (
    SldprtReadError,
    UnsupportedDocumentKindError,
    UnsupportedIRVersionError,
)
from cad3d_ir_sldprt.preview import PreviewImage, preview_image

ADAPTER_NAME = "cad3d-ir-sldprt"
ADAPTER_VERSION = "0.2.2"

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_MAX_PROPERTIES = 200
_MAX_PROPERTY_CHARS = 2000
_SEVERITIES = {"info", "warning", "error"}
# sldkit display diagnostics that have a stable adapter code.
_DISPLAY_DIAGNOSTICS: dict[str, tuple[str, str, str]] = {
    "display.stream_unreadable": (
        "SLDPRT_DISPLAY_STREAM_UNREADABLE",
        "warning",
        "skipped",
    ),
    "display.envelope_unsupported": (
        "SLDPRT_DISPLAY_ENCODING_UNSUPPORTED",
        "warning",
        "skipped",
    ),
    "display.multiple_streams": (
        "SLDPRT_CONFIGURATION_UNRESOLVED",
        "warning",
        "preserved",
    ),
    "display.vertex_budget_exceeded": (
        "SLDPRT_VERTEX_BUDGET_EXCEEDED",
        "warning",
        "skipped",
    ),
    "display.normals_derived": ("SLDPRT_NORMALS_DERIVED", "info", "approximated"),
    "display.assembly_placement_unverified": (
        "SLDPRT_ASSEMBLY_PLACEMENT_UNVERIFIED",
        "warning",
        "preserved",
    ),
}
_HANDLED_DISPLAY_CODES = frozenset(
    {"display.no_display_data", "display.document_kind_unsupported"}
)


def sldkit_version() -> str:
    try:
        return version("sldkit")
    except PackageNotFoundError:  # pragma: no cover - installed as a dependency
        return str(getattr(sldkit, "__version__", "unknown"))


def solidworks_year_estimate(internal_version: int | None) -> int | None:
    """Estimate the SOLIDWORKS release year from the document's internal version.

    Observed files place SOLIDWORKS 2015 at 8000 and SOLIDWORKS 2026 at 19000,
    one thousand per release, so the year is ``2007 + version // 1000``.
    """
    if internal_version is None or internal_version < 1000:
        return None
    return 2007 + internal_version // 1000


def _enum_value(value: Any) -> Any:
    """Unwrap sldkit ``SourcedValue`` and enum members to plain values."""
    for _ in range(3):
        if value is None:
            return None
        inner = getattr(value, "value", None)
        if inner is None or inner is value:
            return value
        value = inner
    return value


def slug(value: str) -> str:
    text = _SLUG_RE.sub("-", value.casefold()).strip("-")
    return text[:48] or "part"


def hash_file(path: Path) -> tuple[str, int]:
    digest = sha256()
    byte_length = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            byte_length += len(chunk)
    return digest.hexdigest(), byte_length


def _iter_bytes(payload: bytes, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
    for offset in range(0, len(payload), chunk_size):
        yield payload[offset : offset + chunk_size]


@dataclass(slots=True)
class SldprtSource:
    """Everything sldkit read from one document before the mapping."""

    path: Path
    digest: str
    byte_length: int
    display: Any
    document: Any | None
    document_kind: str
    envelope: str
    internal_version: int | None
    preview: PreviewImage | None = None
    diagnostics: list[ImportDiagnostic] = field(default_factory=list)
    limit_profile: str = "service"

    @property
    def has_mesh(self) -> bool:
        mesh = self.display.mesh
        return (
            mesh is not None
            and mesh.triangle_count > 0
            and len(self.display.positions) >= 36
            and len(self.display.indices) >= 12
        )

    @property
    def display_status(self) -> str:
        return str(_enum_value(self.display.status))


def read_source(
    path: Path,
    *,
    limit_profile: str,
    read_properties: bool,
    include_preview: bool,
) -> SldprtSource:
    """Run sldkit's display and metadata readers and merge what they found."""
    source_path = Path(path)
    try:
        display = sldkit.decode_display_mesh_file(source_path, profile=limit_profile)
    except sldkit.SldkitError as exc:
        raise SldprtReadError(f"sldkit could not read {source_path}: {exc}") from exc
    digest, byte_length = hash_file(source_path)
    source_id_placeholder = f"source-sldprt-{digest[:12]}"

    document = None
    diagnostics: list[ImportDiagnostic] = []
    if read_properties or include_preview or display.mesh is None:
        try:
            parsed = sldkit.parse_file(source_path, profile=limit_profile)
            document = parsed.document
        except sldkit.SldkitError as exc:
            diagnostics.append(
                ImportDiagnostic(
                    code="SLDPRT_PROPERTIES_NOT_READ",
                    severity="warning",
                    action="skipped",
                    message=f"Document metadata could not be read: {exc}",
                    source_document_id=source_id_placeholder,
                )
            )

    mesh = display.mesh
    if mesh is not None:
        document_kind = str(_enum_value(mesh.document_kind))
        envelope = str(_enum_value(mesh.envelope))
        internal_version = mesh.internal_version
    elif document is not None:
        document_kind = str(_enum_value(document.document_kind))
        envelope = str(_enum_value(document.envelope))
        internal_version = _enum_value(document.internal_version)
    else:
        messages = [item.message for item in display.diagnostics]
        raise SldprtReadError(
            f"not a readable SolidWorks document: {source_path}"
            + (f" ({messages[0]})" if messages else "")
        )
    if internal_version is None and document is not None:
        internal_version = _enum_value(document.internal_version)

    source = SldprtSource(
        path=source_path,
        digest=digest,
        byte_length=byte_length,
        display=display,
        document=document,
        document_kind=document_kind,
        envelope=envelope,
        internal_version=None if internal_version is None else int(internal_version),
        diagnostics=diagnostics,
        limit_profile=limit_profile,
    )
    if include_preview and document is not None:
        source.preview = _read_preview(source, limit_profile)
    return source


def _preview_resource(document: Any) -> Any | None:
    if document.preview is not None:
        return document.preview
    for configuration in document.configurations:
        if configuration.preview is not None:
            return configuration.preview
    return None


def _read_preview(source: SldprtSource, limit_profile: str) -> PreviewImage | None:
    resource = _preview_resource(source.document)
    if resource is None:
        return None
    try:
        extracted = sldkit.extract_resource_file(
            source.path, resource, profile=limit_profile
        )
        return preview_image(bytes(extracted.data), str(_enum_value(resource.kind)))
    except (sldkit.SldkitError, ValueError) as exc:
        source.diagnostics.append(
            ImportDiagnostic(
                code="SLDPRT_PREVIEW_UNREADABLE",
                severity="warning",
                action="skipped",
                message=f"The saved preview image could not be read: {exc}",
                source_document_id=f"source-sldprt-{source.digest[:12]}",
            )
        )
        return None


def _configuration_names(document: Any | None) -> list[str]:
    names: list[str] = []
    if document is None:
        return names
    for configuration in document.configurations:
        name = _enum_value(configuration.name)
        if isinstance(name, str) and name and name not in names:
            names.append(name)
    return names


def _custom_properties(document: Any | None) -> dict[str, str]:
    """Flatten sldkit's property records to ``name: value`` (first value wins)."""
    properties: dict[str, str] = {}
    if document is None:
        return properties
    for record in document.properties:
        if len(properties) >= _MAX_PROPERTIES:
            break
        name = _enum_value(record.name)
        value = _enum_value(record.raw_value)
        state = str(_enum_value(record.value_state))
        if not isinstance(name, str) or not name or name in properties:
            continue
        if state != "present" or not isinstance(value, str):
            continue
        properties[name] = value[:_MAX_PROPERTY_CHARS]
    return properties


def _length_unit_name(document: Any | None) -> str | None:
    units = getattr(document, "units", None)
    length = getattr(units, "length", None)
    name = _enum_value(getattr(length, "name", None))
    return name if isinstance(name, str) else None


def _display_diagnostics(
    source: SldprtSource, source_id: str, *, skip: frozenset[str] = frozenset()
) -> list[ImportDiagnostic]:
    diagnostics: list[ImportDiagnostic] = []
    for item in source.display.diagnostics:
        code = str(item.code)
        if (
            code in _HANDLED_DISPLAY_CODES
            or code in skip
            or not code.startswith("display.")
        ):
            continue
        mapped = _DISPLAY_DIAGNOSTICS.get(code)
        if mapped is None:
            severity = str(_enum_value(item.severity))
            mapped = (
                "SLDPRT_DISPLAY_" + code.removeprefix("display.").upper(),
                severity if severity in _SEVERITIES else "warning",
                "preserved",
            )
        adapter_code, severity, action = mapped
        details: dict[str, Any] = {"sldkit_code": code}
        if item.stream_path:
            details["stream_path"] = str(item.stream_path)
        details.update({str(k): str(v) for k, v in dict(item.details).items()})
        diagnostics.append(
            ImportDiagnostic(
                code=adapter_code,
                severity=severity,  # type: ignore[arg-type]
                action=action,  # type: ignore[arg-type]
                message=str(item.message),
                source_document_id=source_id,
                details=details,
            )
        )
    return diagnostics


@dataclass(frozen=True, slots=True)
class _AssemblyItem:
    """A component instance with the IR identifiers it will take."""

    instance: PlacedInstance
    suffix: str
    product_id: str
    configuration_id: str
    occurrence_id: str
    body_id: str

    @property
    def occurs(self) -> bool:
        return not self.instance.suppressed

    @property
    def renders(self) -> bool:
        return (
            bool(self.instance.part_indices)
            and self.occurs
            and not self.instance.hidden
        )


def _assembly_items(
    layout: AssemblyLayout, name_slug: str, key: str
) -> list[_AssemblyItem]:
    items: list[_AssemblyItem] = []
    for index, instance in enumerate(layout.instances, start=1):
        suffix = f"{slug(instance.record.name)}-{instance.record.number}-{key}-{index}"
        items.append(
            _AssemblyItem(
                instance=instance,
                suffix=suffix,
                product_id=f"product-{suffix}",
                configuration_id=f"configuration-{suffix}",
                occurrence_id=f"occurrence-{suffix}",
                body_id=f"body-{suffix}",
            )
        )
    return items


def _assembly_diagnostics(
    layout: AssemblyLayout | None,
    layout_error: str | None,
    items: list[_AssemblyItem],
    source_id: str,
) -> list[ImportDiagnostic]:
    diagnostics: list[ImportDiagnostic] = []
    if layout is None:
        diagnostics.append(
            ImportDiagnostic(
                code="SLDASM_PLACEMENT_UNRESOLVED",
                severity="warning",
                action="approximated",
                message=(
                    "Component placement could not be read from the assembly; the "
                    "display tables are shown at their stored coordinates."
                ),
                source_document_id=source_id,
                details={"reason": layout_error or "no display stream"},
            )
        )
        return diagnostics
    placed = len(layout.placed_instances)
    if placed:
        diagnostics.append(
            ImportDiagnostic(
                code="SLDASM_PLACEMENT_FROM_COMPONENT_TRANSFORMS",
                severity="info",
                action="preserved",
                message=(
                    f"{placed} component instances were placed with the assembly's "
                    "stored component transforms (swTransform)."
                ),
                source_document_id=source_id,
                details={"instances": placed, "components": layout.components},
            )
        )
    unplaced = layout.unplaced_parts
    if unplaced:
        diagnostics.append(
            ImportDiagnostic(
                code="SLDASM_PLACEMENT_UNRESOLVED",
                severity="warning",
                action="approximated",
                message=(
                    f"{len(unplaced)} face tables could not be assigned to a placed "
                    "component (nested subassemblies or unmatched instance records); "
                    "they are shown at their stored coordinates."
                ),
                source_document_id=source_id,
                details={
                    "tables": len(unplaced),
                    "nested_records": layout.nested_records,
                    "unassigned_tables": len(layout.unassigned_parts),
                },
            )
        )
    hidden = sum(
        1 for item in items if item.instance.hidden and not item.instance.suppressed
    )
    suppressed = sum(1 for item in items if item.instance.suppressed)
    if hidden or suppressed:
        diagnostics.append(
            ImportDiagnostic(
                code="SLDASM_COMPONENTS_SKIPPED",
                severity="info",
                action="skipped",
                message=(
                    f"{hidden} hidden and {suppressed} suppressed component instances "
                    "are left out of the visualization."
                ),
                source_document_id=source_id,
                details={"hidden": hidden, "suppressed": suppressed},
            )
        )
    return diagnostics


class SldprtImporter:
    """Importer for SOLIDWORKS parts (``.SLDPRT``) and assemblies (``.SLDASM``).

    Assemblies are placed from what the file stores: each face table is
    assigned to its component instance through the instance records of the
    display stream and moved with that component's ``swTransform`` (see
    :mod:`cad3d_ir_sldprt.assembly`). ``accept_assemblies=False`` rejects
    ``.SLDASM`` documents instead. ``read_properties`` controls the metadata
    parse (configuration names, custom properties, units; assemblies need it
    for the component transforms); ``include_preview`` writes the saved
    preview image as a package resource. ``limit_profile`` is the sldkit limit
    profile (``"service"`` is bounded for servers, ``"desktop"`` is larger).
    """

    format_name = "sldprt"
    suffixes = frozenset({".sldprt"})

    def __init__(
        self,
        *,
        accept_assemblies: bool = True,
        read_properties: bool = True,
        include_preview: bool = True,
        limit_profile: str = "service",
    ) -> None:
        self.accept_assemblies = accept_assemblies
        self.read_properties = read_properties
        self.include_preview = include_preview
        self.limit_profile = limit_profile

    @property
    def accepted_suffixes(self) -> frozenset[str]:
        if self.accept_assemblies:
            return self.suffixes | {".sldasm"}
        return self.suffixes

    def probe(self, path: Path) -> float:
        """Return a bounded confidence from sldkit's signature probe."""
        source_path = Path(path)
        try:
            if not source_path.is_file():
                return 0.0
            probe = sldkit.probe_file(source_path, profile=self.limit_profile)
        except (OSError, sldkit.SldkitError):
            return 0.0
        if str(_enum_value(probe.status)) != "recognized":
            return 0.0
        score = 0.0
        if source_path.suffix.lower() in self.accepted_suffixes:
            score += 0.4
        envelope = str(_enum_value(probe.envelope))
        if envelope == "modern_chunk":
            score += 0.5
        elif envelope == "ole2_cfb":
            # Many formats share the OLE2 container; the suffix carries the rest.
            score += 0.2
        if str(_enum_value(probe.confidence)) == "high":
            score += 0.1
        return min(score, 1.0)

    def import_file(
        self,
        path: Path,
        *,
        options: ImportOptions,
        resources: ResourceWriter,
    ) -> ImportResult:
        """Map one SOLIDWORKS document to a single-product package."""
        source_path = Path(path)
        if options.ir_version != CURRENT_IR_VERSION:
            raise UnsupportedIRVersionError(
                f"{ADAPTER_NAME} supports IR {CURRENT_IR_VERSION}, "
                f"not {options.ir_version}"
            )
        if not source_path.is_file():
            raise SldprtReadError(f"SLDPRT source is not a file: {source_path}")

        source = read_source(
            source_path,
            limit_profile=self.limit_profile,
            read_properties=self.read_properties,
            include_preview=self.include_preview,
        )
        if source.document_kind == "drawing":
            raise UnsupportedDocumentKindError(
                f"SOLIDWORKS drawings are not 3D models: {source_path}"
            )
        if source.document_kind == "assembly" and not self.accept_assemblies:
            raise UnsupportedDocumentKindError(
                "SOLIDWORKS assemblies are not accepted by this importer "
                f"(accept_assemblies=False): {source_path}"
            )
        return _map_source(source, options=options, resources=resources)


def _map_source(
    source: SldprtSource,
    *,
    options: ImportOptions,
    resources: ResourceWriter,
) -> ImportResult:
    key = source.digest[:12]
    stem = source.path.stem or "part"
    name_slug = slug(stem)
    is_assembly = source.document_kind == "assembly"
    source_format = "sldasm" if is_assembly else "sldprt"
    source_id = f"source-{source_format}-{key}"
    product_id = f"product-{name_slug}-{key}"
    configuration_id = f"configuration-{name_slug}-{key}"
    body_id = f"body-{name_slug}-{key}"
    representation_id = f"representation-{name_slug}-{key}"
    resource_id = f"resource-{name_slug}-{key}"
    visualization_id = f"visualization-{name_slug}-{key}"
    appearance_id = f"appearance-{name_slug}-{key}"
    preview_resource_id = f"resource-preview-{name_slug}-{key}"

    year = solidworks_year_estimate(source.internal_version)
    configuration_names = _configuration_names(source.document)
    custom_properties = _custom_properties(source.document)
    length_unit = _length_unit_name(source.document)
    mesh = source.display.mesh
    streams = [] if mesh is None else list(mesh.streams)

    layout: AssemblyLayout | None = None
    layout_error: str | None = None
    if is_assembly and source.has_mesh:
        try:
            layout = resolve_layout(
                source.path,
                source.display,
                source.document,
                limit_profile=source.limit_profile,
            )
        except sldkit.SldkitError as exc:
            layout_error = str(exc)
    assembly_items = (
        _assembly_items(layout, name_slug, key) if layout is not None else []
    )
    rendered_items = [item for item in assembly_items if item.renders]
    root_body_needed = not rendered_items or bool(layout and layout.unplaced_parts)

    source_properties: dict[str, Any] = {
        "envelope": source.envelope,
        "document_kind": source.document_kind,
        "internal_version": source.internal_version,
        "solidworks_year_estimate": year,
        "configuration_names": configuration_names,
        "custom_properties": custom_properties,
        "display_streams": [
            {
                "entry_id": str(stream.entry_id),
                "stream_path": str(stream.stream_path),
                "part_count": int(stream.part_count),
            }
            for stream in streams
        ],
    }
    if length_unit is not None:
        source_properties["document_length_unit"] = length_unit
    source_document = SourceDocument(
        id=source_id,
        name=source.path.name,
        format=source_format,
        version=None if year is None else f"SOLIDWORKS {year}",
        uri=source.path.name,
        sha256=source.digest,
        properties=source_properties,
    )
    source_ref = SourceRef(
        document_id=source_id,
        source_kind="sldkit-display-mesh",
        source_path=str(streams[0].stream_path) if streams else None,
        source_id=str(streams[0].entry_id) if streams else None,
    )

    diagnostics: list[ImportDiagnostic] = [
        ImportDiagnostic(
            code=item.code,
            severity=item.severity,
            action=item.action,
            message=item.message,
            source_document_id=source_id,
            details=item.details,
        )
        for item in source.diagnostics
    ]
    diagnostics.extend(
        _display_diagnostics(
            source,
            source_id,
            skip=(
                frozenset({"display.assembly_placement_unverified"})
                if layout is not None and layout.placed_instances
                else frozenset()
            ),
        )
    )

    bodies: list[Body] = []
    representations: list[GeometryRepresentation] = []
    resource_records: list[Any] = []
    appearances: list[Appearance] = []
    visualizations: list[Visualization] = []
    display_glb: DisplayGlb | None = None
    color_counts: Counter[Any] = Counter()
    uncolored_parts = 0
    glb_length = 0
    metadata: dict[str, Any] = {
        "adapter": ADAPTER_NAME,
        "adapter_version": ADAPTER_VERSION,
        "sldkit_version": sldkit_version(),
        "source_format": source_format,
        "display_status": source.display_status,
        "envelope": source.envelope,
        "internal_version": source.internal_version,
        "solidworks_year_estimate": year,
    }

    if source.has_mesh:
        if options.include_visualization_mesh:
            root_extras = {
                "cad3d_ir_sldprt": {
                    "source_format": source_format,
                    "sldkit_version": sldkit_version(),
                    "display_status": source.display_status,
                    "internal_version": source.internal_version,
                }
            }
            if layout is not None and rendered_items:
                display_glb = build_assembly_glb(
                    source.display,
                    name=stem,
                    root_product_id=product_id,
                    root_body_id=body_id,
                    items=[
                        (
                            item.instance,
                            item.product_id,
                            item.occurrence_id,
                            item.body_id,
                        )
                        for item in rendered_items
                    ],
                    unplaced_parts=layout.unplaced_parts,
                    include_edges=options.mesh_include_edges,
                    generator=f"{ADAPTER_NAME} {ADAPTER_VERSION}",
                    root_extras=root_extras,
                )
            else:
                display_glb = build_display_glb(
                    source.display,
                    name=stem,
                    body_id=body_id,
                    product_id=product_id,
                    include_edges=options.mesh_include_edges,
                    generator=f"{ADAPTER_NAME} {ADAPTER_VERSION}",
                    root_extras=root_extras,
                )
            color_counts = display_glb.colors
            uncolored_parts = display_glb.uncolored_parts
            glb_length = len(display_glb.payload)
        else:
            for part in mesh.parts:
                color = part.color
                if color is None:
                    uncolored_parts += 1
                else:
                    color_counts[tuple(float(c) for c in color)] += 1

        dominant = color_counts.most_common(1)[0][0] if color_counts else None
        if dominant is not None:
            appearances.append(
                Appearance(
                    id=appearance_id,
                    name=f"{stem} appearance",
                    base_color=dominant,
                    source=source_ref,
                )
            )
        representation_ids: list[str] = []
        bounds: BBox3 | None = None
        if display_glb is not None:
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
                properties={"sldkit_version": sldkit_version()},
            )
            resource_records.append(resource)
            mesh_metadata = {
                "sldkit_version": sldkit_version(),
                "display_status": source.display_status,
                "triangles": display_glb.triangle_count,
                "line_segments": display_glb.line_segment_count,
                "parts": display_glb.part_count,
            }
            if root_body_needed:
                representations.append(
                    GeometryRepresentation(
                        id=representation_id,
                        name=f"{stem} display mesh",
                        kind="mesh",
                        role="visualization",
                        fidelity="derived",
                        resource_id=resource.id,
                        units=UnitSystem(length_unit="mm"),
                        bounds=bounds,
                        derivation=RepresentationDerivation(
                            relation="tessellated",
                            method=(
                                "SOLIDWORKS saved display tessellation "
                                "(sldkit decode_display_mesh) to glTF binary"
                            ),
                            source=source_ref,
                            metadata=mesh_metadata,
                        ),
                        topology_namespace=f"topology-{name_slug}-{resource.sha256[:12]}",
                        source=source_ref,
                    )
                )
                representation_ids.append(representation_id)
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
                        source_representation_id=(
                            representation_id if root_body_needed else None
                        ),
                        method="cad3d-ir GlbBuilder from sldkit display meshes",
                        source=source_ref,
                        metadata={"include_edges": options.mesh_include_edges},
                    ),
                    statistics={
                        "triangles": display_glb.triangle_count,
                        "line_segments": display_glb.line_segment_count,
                        "vertices": display_glb.vertex_count,
                        "nodes": display_glb.node_count,
                        "meshes": display_glb.mesh_count,
                        "parts": display_glb.part_count,
                    },
                    source=source_ref,
                )
            )
        if root_body_needed:
            bodies.append(
                Body(
                    id=body_id,
                    name=f"{stem} body",
                    representation_ids=representation_ids,
                    appearance_id=appearance_id if dominant is not None else None,
                    source=source_ref,
                )
            )
        for item in rendered_items:
            item_colors = instance_colors(mesh, item.instance)
            saved = {k: v for k, v in item_colors.items() if k is not None}
            item_appearance: str | None = None
            if saved:
                item_appearance = f"appearance-{item.suffix}"
                appearances.append(
                    Appearance(
                        id=item_appearance,
                        name=f"{item.instance.label} appearance",
                        base_color=max(saved.items(), key=lambda kv: kv[1])[0],
                        source=source_ref,
                    )
                )
            bodies.append(
                Body(
                    id=item.body_id,
                    name=f"{item.instance.label} body",
                    appearance_id=item_appearance,
                    source=source_ref,
                )
            )
        diagnostics.append(
            ImportDiagnostic(
                code="SLDPRT_DISPLAY_MESH_ONLY",
                severity="info",
                action="preserved",
                message=(
                    "Geometry comes from the display meshes SOLIDWORKS saved with "
                    "the document; the exact B-Rep is not read, so faces keep the "
                    "saved image quality and no exact surfaces are available."
                ),
                source_document_id=source_id,
                details={
                    "parts": len(mesh.parts),
                    "triangles": int(mesh.triangle_count),
                    "display_status": source.display_status,
                },
            )
        )
        if not color_counts:
            diagnostics.append(
                ImportDiagnostic(
                    code="SLDPRT_APPEARANCE_NOT_SAVED",
                    severity="info",
                    action="skipped",
                    message=(
                        "No appearance is saved with the display data; the "
                        "default face colour is used."
                    ),
                    source_document_id=source_id,
                )
            )
        elif len(color_counts) > 1 or uncolored_parts:
            diagnostics.append(
                ImportDiagnostic(
                    code="SLDPRT_COLORS_NOT_MAPPED",
                    severity="info",
                    action="approximated",
                    message=(
                        f"{len(color_counts)} saved face colours are kept as "
                        "visualization materials; the body appearance uses the "
                        "most common one."
                    ),
                    source_document_id=source_id,
                    details={
                        "color_count": len(color_counts),
                        "uncolored_parts": uncolored_parts,
                    },
                )
            )
        if is_assembly:
            diagnostics.extend(
                _assembly_diagnostics(layout, layout_error, assembly_items, source_id)
            )
    elif source.preview is not None:
        diagnostics.append(
            ImportDiagnostic(
                code="SLDPRT_PREVIEW_ONLY",
                severity="warning",
                action="skipped",
                message=(
                    "No display mesh could be read from this document; only the "
                    "saved preview image and the document metadata are available."
                ),
                source_document_id=source_id,
                details={
                    "envelope": source.envelope,
                    "display_status": source.display_status,
                    "legacy": source.envelope == "ole2_cfb",
                },
            )
        )
    else:
        diagnostics.append(
            ImportDiagnostic(
                code="SLDPRT_NO_DISPLAY_DATA",
                severity="error",
                action="unresolved",
                message=(
                    "The document carries neither readable display meshes nor a "
                    "preview image; only metadata was read."
                ),
                source_document_id=source_id,
                details={
                    "envelope": source.envelope,
                    "display_status": source.display_status,
                },
            )
        )
        if options.strict:
            raise SldprtReadError(
                f"no display data in SOLIDWORKS document: {source.path}"
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
                source_kind="sldkit-preview-image",
                source_id=preview.source_kind,
            ),
            properties={
                "role": "preview-image",
                "sldkit_kind": preview.source_kind,
                "width": preview.width,
                "height": preview.height,
            },
        )
        resource_records.append(preview_record)
        preview_length = len(preview.data)
        metadata["preview_resource_id"] = preview_record.id

    product_definitions = [
        ProductDefinition(
            id=product_id,
            name=stem,
            kind="assembly" if is_assembly else "part",
            source_document_id=source_id,
            default_configuration_id=configuration_id,
            source=source_ref,
        )
    ]
    configurations = [
        Configuration(
            id=configuration_id,
            name=configuration_names[0] if configuration_names else "Default",
            product_id=product_id,
            body_ids=[body.id for body in bodies if body.id == body_id],
            occurrence_ids=[
                item.occurrence_id for item in assembly_items if item.occurs
            ],
            source=source_ref,
        )
    ]
    occurrences: list[Occurrence] = []
    for item in assembly_items:
        if not item.occurs:
            continue
        record = item.instance.record
        component_ref = SourceRef(
            document_id=source_id,
            source_kind="sldkit-assembly-component",
            source_id=record.path,
            source_path=str(streams[record.stream_index].stream_path)
            if streams
            else None,
        )
        product_definitions.append(
            ProductDefinition(
                id=item.product_id,
                name=record.name,
                kind="part",
                source_document_id=source_id,
                default_configuration_id=item.configuration_id,
                source=component_ref,
            )
        )
        configurations.append(
            Configuration(
                id=item.configuration_id,
                name="Default",
                product_id=item.product_id,
                body_ids=[item.body_id] if item.renders else [],
                source=component_ref,
            )
        )
        occurrences.append(
            Occurrence(
                id=item.occurrence_id,
                name=item.instance.label,
                product_id=item.product_id,
                configuration_id=item.configuration_id,
                transform=Transform(matrix=item.instance.matrix),
                visible=not item.instance.hidden,
                suppressed=False,
                source=component_ref,
            )
        )
    if layout is not None:
        metadata["assembly_name"] = layout.assembly_name
        metadata["placed_instances"] = len(layout.placed_instances)
        metadata["unplaced_parts"] = len(layout.unplaced_parts)

    package = CadPackage(
        format="cad3d-ir",
        version=CURRENT_IR_VERSION,
        id=f"package-{source_format}-{key}",
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
        appearances=appearances,
        visualizations=visualizations,
        roots=[RootSelection(product_id=product_id, configuration_id=configuration_id)],
        metadata=metadata,
    )
    if options.validate:
        validate_package(package)

    statistics: dict[str, Any] = {
        "source_byte_length": source.byte_length,
        "source_format": source_format,
        "envelope": source.envelope,
        "internal_version": source.internal_version,
        "solidworks_year_estimate": year,
        "display_status": source.display_status,
        "display_streams": len(streams),
        "display_parts": 0 if mesh is None else len(mesh.parts),
        "configurations": len(configuration_names),
        "custom_properties": len(custom_properties),
        "products": len(product_definitions),
        "parts": len(product_definitions) - 1 if is_assembly else 1,
        "assemblies": 1 if is_assembly else 0,
        "occurrences": len(occurrences),
        "instances": len(assembly_items),
        "placed_instances": 0 if layout is None else len(layout.placed_instances),
        "unplaced_parts": 0 if layout is None else len(layout.unplaced_parts),
        "hidden_instances": sum(1 for item in assembly_items if item.instance.hidden),
        "suppressed_instances": sum(
            1 for item in assembly_items if item.instance.suppressed
        ),
        "bodies": len(bodies),
        "resources": len(resource_records),
        "triangles": 0 if display_glb is None else display_glb.triangle_count,
        "line_segments": 0 if display_glb is None else display_glb.line_segment_count,
        "vertices": 0 if display_glb is None else display_glb.vertex_count,
        "visualization_byte_length": glb_length,
        "preview_byte_length": preview_length,
    }
    return ImportResult(package=package, diagnostics=diagnostics, statistics=statistics)


__all__ = [
    "ADAPTER_NAME",
    "ADAPTER_VERSION",
    "SldprtImporter",
    "SldprtSource",
    "read_source",
    "sldkit_version",
    "solidworks_year_estimate",
]
