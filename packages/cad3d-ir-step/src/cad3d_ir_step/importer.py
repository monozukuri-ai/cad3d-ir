"""STEP (AP203 / AP214 / AP242) to cad3d-ir mapping through Open CASCADE XDE."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cad3d_ir import ImportDiagnostic, ImportResult, SourceDocument, validate_package
from cad3d_ir.constants import CURRENT_IR_VERSION
from cad3d_ir.importers import ImportOptions
from cad3d_ir.io import ResourceWriter
from OCP.IFSelect import IFSelect_RetDone
from OCP.Standard import Standard_Failure
from OCP.STEPCAFControl import STEPCAFControl_Reader
from OCP.STEPControl import STEPControl_Controller
from OCP.TColStd import TColStd_SequenceOfAsciiString

from cad3d_ir_step.errors import (
    StepReadError,
    UnsupportedIRVersionError,
    UnsupportedStepSchemaError,
)
from cad3d_ir_step.header import inspect_step_header
from cad3d_ir_step.xde import (
    hash_file,
    map_xde,
    mesh_settings_from,
    package_statistics,
    read_xde,
    unit_diagnostics,
    unsupported_feature_diagnostics,
)


def _sequence_values(sequence: Any) -> tuple[str, ...]:
    return tuple(
        sequence.Value(index).ToCString() for index in range(1, sequence.Length() + 1)
    )


def _transfer_step(path: Path, document: Any) -> tuple[str, ...]:
    STEPControl_Controller.Init_s()
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
    return source_length_units


class StepImporter:
    """Importer for STEP Part 21 files (AP203, AP214, AP242) using OCCT XDE.

    ``require_ap242`` restores the 0.1 behaviour of rejecting other schemas
    before any geometry is transferred. ``body_mesh_resources`` controls
    whether, in addition to the package-level visualization, every part body
    also gets its own ``mesh`` representation resource when display meshes
    are requested.
    """

    format_name = "step"
    suffixes = frozenset({".step", ".stp", ".p21"})

    def __init__(
        self,
        *,
        require_ap242: bool = False,
        body_mesh_resources: bool = True,
    ) -> None:
        self.require_ap242 = require_ap242
        self.body_mesh_resources = body_mesh_resources

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
        elif header.schemas:
            score += 0.1
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
        if self.require_ap242 and not header.is_ap242:
            declared = ", ".join(header.schemas) if header.schemas else "none"
            raise UnsupportedStepSchemaError(
                f"AP242 import was required but the FILE_SCHEMA is {declared}"
            )

        source_digest, source_size = hash_file(source_path)
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
                    severity="info",
                    action="preserved",
                    message=(
                        "The STEP file declares a schema other than AP242; product "
                        "structure, names and colours were read as available."
                    ),
                    source_document_id=source_id,
                    details={"schemas": list(header.schemas)},
                )
            )

        try:
            xde = read_xde(source_path, transfer=_transfer_step)
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
                package = map_xde(
                    xde,
                    source=source,
                    source_digest=source_digest,
                    resources=resources,
                    diagnostics=diagnostics,
                    options=options,
                    prefix="STEP",
                    resource_properties={"step_schemas": list(header.schemas)},
                    mesh_settings=mesh_settings_from(
                        options, body_resources=self.body_mesh_resources
                    ),
                )
            finally:
                xde.close()
        except Standard_Failure as exc:
            raise StepReadError(
                f"OCCT failed while importing {source_path}: {exc}"
            ) from exc

        diagnostics.extend(
            unsupported_feature_diagnostics(
                source_id=source_id,
                counts=xde.unsupported_counts,
                prefix="STEP",
            )
        )
        diagnostics.extend(
            unit_diagnostics(
                source_id=source_id,
                source_length_units=xde.source_length_units,
                prefix="STEP",
            )
        )

        if options.validate:
            validate_package(package)

        statistics: dict[str, Any] = {
            "source_byte_length": source_size,
            "source_length_units": list(xde.source_length_units),
            "step_schemas": list(header.schemas),
            **package_statistics(package),
        }
        return ImportResult(
            package=package,
            diagnostics=diagnostics,
            statistics=statistics,
        )
