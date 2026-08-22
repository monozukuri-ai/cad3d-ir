"""STEP AP242 to cad3d-ir mapping through Open CASCADE XDE."""

from __future__ import annotations

import os
import re
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
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
    ImportResult,
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
from cad3d_ir.constants import CURRENT_IR_VERSION
from cad3d_ir.importers import ImportOptions
from cad3d_ir.io import ResourceWriter
from OCP.Bnd import Bnd_Box
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepTools import BRepTools
from OCP.IFSelect import IFSelect_RetDone
from OCP.Standard import Standard_Failure
from OCP.STEPCAFControl import STEPCAFControl_Reader
from OCP.STEPControl import STEPControl_Controller
from OCP.TCollection import TCollection_AsciiString, TCollection_ExtendedString
from OCP.TColStd import TColStd_SequenceOfAsciiString
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

from cad3d_ir_step.errors import (
    StepReadError,
    UnsupportedIRVersionError,
    UnsupportedStepSchemaError,
)
from cad3d_ir_step.header import StepHeader, inspect_step_header

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_IDENTITY_3X4 = (
    (1.0, 0.0, 0.0, 0.0),
    (0.0, 1.0, 0.0, 0.0),
    (0.0, 0.0, 1.0, 0.0),
)
_MM_NAMES = {"millimetre", "millimeter", "mm"}


@dataclass(frozen=True, slots=True)
class _XdeNode:
    depth: int
    path: str
    label_entry: str
    definition_entry: str
    definition_label: Any
    definition_name: str
    occurrence_name: str
    is_assembly: bool
    local_matrix: tuple[
        tuple[float, float, float, float],
        tuple[float, float, float, float],
        tuple[float, float, float, float],
        tuple[float, float, float, float],
    ]
    visible: bool
    color: tuple[float, float, float, float] | None


@dataclass(slots=True)
class _XdeReadResult:
    application: Any
    document: Any
    nodes: list[_XdeNode]
    source_length_units: tuple[str, ...]
    unsupported_counts: dict[str, int]

    def close(self) -> None:
        """Drop Python references to the transient, non-session XDE document."""
        self.nodes.clear()
        self.document = None
        self.application = None


class StepImporter:
    """Reference importer for STEP AP242 Part 21 files using OCCT XDE."""

    format_name = "step-ap242"
    suffixes = frozenset({".step", ".stp", ".p21"})

    def probe(self, path: Path) -> float:
        """Return a bounded confidence score without transferring geometry."""
        try:
            header = inspect_step_header(path)
        except OSError:
            return 0.0

        score = 0.0
        if path.suffix.lower() in self.suffixes:
            score += 0.15
        if header.is_part21:
            score += 0.55
        if header.schemas:
            score += 0.1
        if header.is_ap242:
            score += 0.2
        return min(score, 1.0)

    def import_file(
        self,
        path: Path,
        *,
        options: ImportOptions,
        resources: ResourceWriter,
    ) -> ImportResult:
        """Read one STEP file and map its resolved XDE graph to common IR."""
        source_path = Path(path)
        if options.ir_version != CURRENT_IR_VERSION:
            raise UnsupportedIRVersionError(
                f"cad3d-ir-step supports IR {CURRENT_IR_VERSION}, "
                f"not {options.ir_version}"
            )
        if not source_path.is_file():
            raise StepReadError(f"STEP source is not a file: {source_path}")

        header = inspect_step_header(source_path)
        if not header.is_part21:
            raise StepReadError(f"not a STEP Part 21 file: {source_path}")
        if options.strict and not header.is_ap242:
            declared = ", ".join(header.schemas) if header.schemas else "none"
            raise UnsupportedStepSchemaError(
                f"strict AP242 import requires an AP242 FILE_SCHEMA; found {declared}"
            )

        source_digest, source_size = _hash_file(source_path)
        source_id = f"source-step-{source_digest[:12]}"
        source = SourceDocument(
            id=source_id,
            name=source_path.name,
            format="step-ap242" if header.is_ap242 else "step",
            version=" | ".join(header.schemas) or None,
            uri=source_path.name,
            sha256=source_digest,
            properties={"step_schemas": list(header.schemas)},
        )

        diagnostics: list[ImportDiagnostic] = []
        if not header.is_ap242:
            diagnostics.append(
                ImportDiagnostic(
                    code="STEP_SCHEMA_NOT_AP242",
                    severity="warning",
                    action="preserved",
                    message=(
                        "Imported a non-AP242 STEP file because strict mode is off."
                    ),
                    source_document_id=source_id,
                    details={"schemas": list(header.schemas)},
                )
            )
        if options.include_visualization_mesh:
            diagnostics.append(
                ImportDiagnostic(
                    code="STEP_VISUALIZATION_MESH_NOT_IMPLEMENTED",
                    severity="warning",
                    action="skipped",
                    message=(
                        "Visualization mesh generation is not implemented by "
                        "this adapter."
                    ),
                    source_document_id=source_id,
                )
            )

        try:
            xde = _read_xde(source_path)
            try:
                source = source.model_copy(
                    update={
                        "properties": {
                            **source.properties,
                            "source_length_units": list(xde.source_length_units),
                            "occt_system_length_unit_mm": 1.0,
                        }
                    }
                )
                package = _map_xde(
                    xde,
                    source=source,
                    source_digest=source_digest,
                    header=header,
                    resources=resources,
                    diagnostics=diagnostics,
                )
            finally:
                xde.close()
        except Standard_Failure as exc:
            raise StepReadError(
                f"OCCT failed while importing {source_path}: {exc}"
            ) from exc

        diagnostics.extend(
            _unsupported_feature_diagnostics(
                source_id=source_id,
                counts=xde.unsupported_counts,
            )
        )
        diagnostics.extend(
            _unit_diagnostics(
                source_id=source_id,
                source_length_units=xde.source_length_units,
            )
        )

        if options.validate:
            validate_package(package)

        statistics: dict[str, Any] = {
            "source_byte_length": source_size,
            "source_length_units": list(xde.source_length_units),
            "step_schemas": list(header.schemas),
            "products": len(package.product_definitions),
            "assemblies": sum(
                product.kind == "assembly" for product in package.product_definitions
            ),
            "parts": sum(
                product.kind == "part" for product in package.product_definitions
            ),
            "occurrences": len(package.occurrences),
            "bodies": len(package.bodies),
            "resources": len(package.resources),
        }
        return ImportResult(
            package=package,
            diagnostics=diagnostics,
            statistics=statistics,
        )


def _hash_file(path: Path) -> tuple[str, int]:
    digest = sha256()
    byte_length = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            byte_length += len(chunk)
    return digest.hexdigest(), byte_length


def _occt_version() -> str:
    for distribution in ("cadquery-ocp-novtk", "cadquery-ocp"):
        try:
            return version(distribution)
        except PackageNotFoundError:
            continue
    return "unknown"


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


def _matrix_from_location(
    location: Any,
) -> tuple[
    tuple[float, float, float, float],
    tuple[float, float, float, float],
    tuple[float, float, float, float],
    tuple[float, float, float, float],
]:
    transform = location.Transformation()
    rows = tuple(
        tuple(float(transform.Value(row, column)) for column in range(1, 5))
        for row in range(1, 4)
    )
    return (*rows, (0.0, 0.0, 0.0, 1.0))


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


def _sequence_values(sequence: Any) -> tuple[str, ...]:
    return tuple(
        sequence.Value(index).ToCString() for index in range(1, sequence.Length() + 1)
    )


def _count_labels(tool: Any, method_name: str) -> int:
    labels = TDF_LabelSequence()
    getattr(tool, method_name)(labels)
    return labels.Length()


def _read_xde(path: Path) -> _XdeReadResult:
    STEPControl_Controller.Init_s()
    application = XCAFApp_Application.GetApplication_s()
    document = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
    application.InitDocument(document)

    try:
        reader = STEPCAFControl_Reader()
        reader.SetNameMode(True)
        reader.SetColorMode(True)
        reader.SetLayerMode(True)
        reader.SetPropsMode(True)
        reader.SetGDTMode(True)
        reader.SetMatMode(True)
        reader.SetViewMode(True)

        status = reader.ReadFile(str(path))
        if status != IFSelect_RetDone:
            raise StepReadError(f"OCCT failed to read STEP file ({status}): {path}")

        lengths = TColStd_SequenceOfAsciiString()
        angles = TColStd_SequenceOfAsciiString()
        solid_angles = TColStd_SequenceOfAsciiString()
        reader.Reader().FileUnits(lengths, angles, solid_angles)
        source_length_units = _sequence_values(lengths)

        # OCCT defines 1.0 as one millimetre for the transfer system unit.
        # Setting it explicitly avoids dependence on process-global unit settings.
        reader.ChangeReader().SetSystemLengthUnit(1.0)
        if not reader.Transfer(document):
            raise StepReadError(f"OCCT failed to transfer STEP data into XDE: {path}")

        explorer = XCAFPrs_DocumentExplorer(
            document,
            XCAFPrs_DocumentExplorerFlags_None,
            XCAFPrs_Style(),
        )
        nodes: list[_XdeNode] = []
        while explorer.More():
            node = explorer.Current()
            definition_entry = _label_entry(node.RefLabel)
            definition_name = _label_name(node.RefLabel) or (
                f"Unnamed {definition_entry}"
            )
            occurrence_name = _label_name(node.Label) or definition_name
            nodes.append(
                _XdeNode(
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
            raise StepReadError(f"STEP transfer produced no XDE product roots: {path}")

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
            "physical_materials": _count_labels(
                material_tool,
                "GetMaterialLabels",
            ),
        }
        return _XdeReadResult(
            application=application,
            document=document,
            nodes=nodes,
            source_length_units=source_length_units,
            unsupported_counts=unsupported_counts,
        )
    except BaseException:
        raise


def _slug(value: str) -> str:
    slug = _SLUG_RE.sub("-", value.casefold()).strip("-")
    return slug[:48] or "item"


def _make_id(prefix: str, name: str, key: str) -> str:
    suffix = sha256(key.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}-{_slug(name)}-{suffix}"


def _source_ref(source_id: str, node: _XdeNode, *, definition: bool) -> SourceRef:
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


def _write_brep(
    *,
    shape: Any,
    resource_id: str,
    resource_uri: str,
    resource_name: str,
    source: SourceRef,
    resources: ResourceWriter,
    header: StepHeader,
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
            properties={
                "occt_version": _occt_version(),
                "step_schemas": list(header.schemas),
            },
        )
    finally:
        temporary_path.unlink(missing_ok=True)


def _shape_bounds(shape: Any) -> BBox3 | None:
    bounds = Bnd_Box()
    BRepBndLib.AddOptimal_s(shape, bounds, False, False)
    if bounds.IsVoid():
        return None
    xmin, ymin, zmin, xmax, ymax, zmax = bounds.Get()
    return BBox3(
        min=(float(xmin), float(ymin), float(zmin)),
        max=(float(xmax), float(ymax), float(zmax)),
    )


def _map_xde(
    xde: _XdeReadResult,
    *,
    source: SourceDocument,
    source_digest: str,
    header: StepHeader,
    resources: ResourceWriter,
    diagnostics: list[ImportDiagnostic],
) -> CadPackage:
    first_by_definition: dict[str, _XdeNode] = {}
    colors_by_definition: dict[
        str,
        set[tuple[float, float, float, float]],
    ] = defaultdict(set)
    for node in xde.nodes:
        first_by_definition.setdefault(node.definition_entry, node)
        if node.color is not None:
            colors_by_definition[node.definition_entry].add(node.color)

    product_ids = {
        key: _make_id("product", node.definition_name, key)
        for key, node in first_by_definition.items()
    }
    configuration_ids = {
        key: _make_id("configuration", node.definition_name, key)
        for key, node in first_by_definition.items()
    }
    occurrence_ids_by_owner: dict[str, list[str]] = defaultdict(list)
    body_ids_by_product: dict[str, list[str]] = defaultdict(list)

    occurrences: list[Occurrence] = []
    roots: list[RootSelection] = []
    stack: list[_XdeNode] = []
    for node in xde.nodes:
        stack = stack[: node.depth]
        if node.depth == 0:
            roots.append(
                RootSelection(
                    product_id=product_ids[node.definition_entry],
                    configuration_id=configuration_ids[node.definition_entry],
                )
            )
            if not _is_identity(node.local_matrix):
                diagnostics.append(
                    ImportDiagnostic(
                        code="STEP_ROOT_TRANSFORM_NOT_REPRESENTABLE",
                        severity="warning",
                        action="skipped",
                        message=(
                            "A non-identity XDE root placement cannot be represented "
                            "by cad3d-ir 0.1 RootSelection."
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
            occurrence_id = _make_id("occurrence", node.occurrence_name, node.path)
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
                    code="STEP_SHAPE_MISSING",
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
        body_id = _make_id("body", node.definition_name, definition_entry)
        representation_id = _make_id(
            "representation",
            node.definition_name,
            definition_entry,
        )
        resource_id = _make_id("resource", node.definition_name, definition_entry)
        source_ref = _source_ref(source.id, node, definition=True)
        resource = _write_brep(
            shape=shape,
            resource_id=resource_id,
            resource_uri=f"geometry/{product_id.removeprefix('product-')}.brep",
            resource_name=f"{node.definition_name} OCCT B-Rep",
            source=source_ref,
            resources=resources,
            header=header,
        )
        resource_records.append(resource)

        color_values = colors_by_definition[definition_entry]
        appearance_id: str | None = None
        if color_values:
            if len(color_values) > 1:
                diagnostics.append(
                    ImportDiagnostic(
                        code="STEP_OCCURRENCE_APPEARANCE_COLLAPSED",
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
            appearance_id = _make_id(
                "appearance",
                node.definition_name,
                definition_entry,
            )
            appearances.append(
                Appearance(
                    id=appearance_id,
                    name=f"{node.definition_name} appearance",
                    base_color=selected_color,
                    source=source_ref,
                )
            )

        representations.append(
            GeometryRepresentation(
                id=representation_id,
                name=f"{node.definition_name} design B-Rep",
                kind="brep",
                role="design",
                fidelity="exact",
                resource_id=resource.id,
                units=UnitSystem(length_unit="mm"),
                bounds=_shape_bounds(shape),
                derivation=RepresentationDerivation(
                    relation="converted",
                    method="OCCT STEPCAFControl AP242 to BRepTools ASCII v1",
                    source=source_ref,
                    metadata={"occt_version": _occt_version()},
                ),
                topology_namespace=_make_id(
                    "topology",
                    node.definition_name,
                    resource.sha256,
                ),
                source=source_ref,
            )
        )
        bodies.append(
            Body(
                id=body_id,
                name=f"{node.definition_name} body",
                representation_ids=[representation_id],
                appearance_id=appearance_id,
                source=source_ref,
            )
        )
        body_ids_by_product[definition_entry].append(body_id)

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
            code="STEP_CONFIGURATIONS_SYNTHESIZED",
            severity="info",
            action="normalized",
            message=(
                "STEP/XDE product definitions were mapped to resolved Default "
                "configurations."
            ),
            source_document_id=source.id,
            details={"configuration_count": len(configurations)},
        )
    )

    root_names = [
        first_by_definition[node.definition_entry].definition_name
        for node in xde.nodes
        if node.depth == 0
    ]
    package_name = root_names[0] if len(root_names) == 1 else source.name
    return CadPackage(
        format="cad3d-ir",
        version=CURRENT_IR_VERSION,
        id=f"package-step-{source_digest[:12]}",
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
        roots=roots,
        metadata={
            "adapter": "cad3d-ir-step",
            "adapter_version": "0.1.0",
            "occt_version": _occt_version(),
            "step_schemas": list(header.schemas),
            "source_length_units": list(xde.source_length_units),
        },
    )


def _is_identity(matrix: tuple[tuple[float, ...], ...]) -> bool:
    return all(
        isclose(actual, expected, rel_tol=0.0, abs_tol=1e-12)
        for row, expected_row in zip(matrix[:3], _IDENTITY_3X4, strict=True)
        for actual, expected in zip(row, expected_row, strict=True)
    )


def _unsupported_feature_diagnostics(
    *,
    source_id: str,
    counts: dict[str, int],
) -> list[ImportDiagnostic]:
    diagnostics: list[ImportDiagnostic] = []
    pmi_count = sum(
        counts[key]
        for key in (
            "dimensions",
            "geometric_tolerances",
            "datums",
            "legacy_dimensional_tolerances",
        )
    )
    if pmi_count:
        diagnostics.append(
            ImportDiagnostic(
                code="STEP_PMI_NOT_MAPPED",
                severity="warning",
                action="skipped",
                message="AP242 PMI/GD&T was read by XDE but is not mapped by IR 0.1.",
                source_document_id=source_id,
                details={
                    key: counts[key]
                    for key in (
                        "dimensions",
                        "geometric_tolerances",
                        "datums",
                        "legacy_dimensional_tolerances",
                    )
                },
            )
        )
    if counts["layers"]:
        diagnostics.append(
            ImportDiagnostic(
                code="STEP_LAYERS_NOT_MAPPED",
                severity="warning",
                action="skipped",
                message="STEP presentation layers are not mapped by IR 0.1.",
                source_document_id=source_id,
                details={"layer_count": counts["layers"]},
            )
        )
    if counts["physical_materials"]:
        diagnostics.append(
            ImportDiagnostic(
                code="STEP_PHYSICAL_MATERIALS_NOT_MAPPED",
                severity="warning",
                action="skipped",
                message="XDE physical materials are not mapped by this adapter yet.",
                source_document_id=source_id,
                details={"material_count": counts["physical_materials"]},
            )
        )
    return diagnostics


def _unit_diagnostics(
    *,
    source_id: str,
    source_length_units: tuple[str, ...],
) -> list[ImportDiagnostic]:
    normalized = {unit.casefold() for unit in source_length_units}
    if not normalized or normalized <= _MM_NAMES:
        return []
    return [
        ImportDiagnostic(
            code="STEP_LENGTH_UNITS_NORMALIZED",
            severity="info",
            action="normalized",
            message="OCCT normalized STEP geometry to package millimetres.",
            source_document_id=source_id,
            details={"source_length_units": list(source_length_units)},
        )
    ]
