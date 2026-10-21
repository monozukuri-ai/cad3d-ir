"""IGES 5.x to cad3d-ir mapping through Open CASCADE XDE."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cad3d_ir import ImportDiagnostic, ImportResult, SourceDocument, validate_package
from cad3d_ir.constants import CURRENT_IR_VERSION
from cad3d_ir.importers import ImportOptions
from cad3d_ir.io import ResourceWriter
from OCP.IFSelect import IFSelect_RetDone
from OCP.IGESCAFControl import IGESCAFControl_Reader
from OCP.IGESControl import IGESControl_Controller
from OCP.Interface import Interface_Static
from OCP.Standard import Standard_Failure

from cad3d_ir_step.errors import StepReadError, UnsupportedIRVersionError
from cad3d_ir_step.xde import (
    hash_file,
    map_xde,
    mesh_settings_from,
    package_statistics,
    read_xde,
    unit_diagnostics,
    unsupported_feature_diagnostics,
)

_HEADER_LIMIT = 512 * 1024
_UNIT_FLAG_NAMES = {
    1: "inch",
    2: "millimetre",
    3: "custom",
    4: "foot",
    5: "mile",
    6: "metre",
    7: "kilometre",
    8: "mil",
    9: "micron",
    10: "centimetre",
    11: "microinch",
}


@dataclass(frozen=True, slots=True)
class IgesHeader:
    """Facts from the start and global sections of an IGES file."""

    is_iges: bool
    unit_flag: int | None
    unit_name: str | None
    product: str | None

    @property
    def length_unit(self) -> str | None:
        if self.unit_flag == 3 and self.unit_name:
            return self.unit_name
        if self.unit_flag is not None:
            return _UNIT_FLAG_NAMES.get(self.unit_flag)
        return None


def _hollerith_fields(text: str) -> list[str]:
    """Split a global section with Hollerith strings on its separator."""
    fields: list[str] = []
    index = 0
    current: list[str] = []
    separator = ","
    while index < len(text):
        char = text[index]
        digits_end = index
        while digits_end < len(text) and text[digits_end].isdigit():
            digits_end += 1
        if digits_end > index and digits_end < len(text) and text[digits_end] == "H":
            length = int(text[index:digits_end])
            start = digits_end + 1
            current.append(text[start : start + length])
            index = start + length
            continue
        if char == separator:
            fields.append("".join(current).strip())
            current = []
        elif char == ";":
            fields.append("".join(current).strip())
            current = []
            break
        else:
            current.append(char)
        index += 1
    if current:
        fields.append("".join(current).strip())
    return fields


def inspect_iges_header(path: Path) -> IgesHeader:
    """Read the bounded prefix and extract unit facts from the global section."""
    with path.open("rb") as stream:
        prefix = stream.read(_HEADER_LIMIT)
    text = prefix.decode("latin-1", errors="replace")
    lines = text.splitlines()
    start_lines = [line for line in lines if len(line) >= 73 and line[72] == "S"]
    global_lines = [line for line in lines if len(line) >= 73 and line[72] == "G"]
    is_iges = bool(start_lines or global_lines) and all(
        len(line) <= 80 or line[72] in "SGDPT" for line in lines[:4]
    )
    if not global_lines:
        return IgesHeader(is_iges=is_iges, unit_flag=None, unit_name=None, product=None)
    global_text = "".join(line[:72] for line in global_lines)
    fields = _hollerith_fields(global_text)
    unit_flag: int | None = None
    unit_name: str | None = None
    product: str | None = None
    if len(fields) >= 14:
        try:
            unit_flag = int(fields[13])
        except ValueError:
            unit_flag = None
    if len(fields) >= 15:
        unit_name = fields[14] or None
    if len(fields) >= 3:
        product = fields[2] or None
    return IgesHeader(
        is_iges=is_iges, unit_flag=unit_flag, unit_name=unit_name, product=product
    )


def _transfer_iges(path: Path, document: Any) -> tuple[str, ...]:
    IGESControl_Controller.Init_s()
    # Geometry is scaled to millimetres so the package unit matches STEP imports.
    Interface_Static.SetCVal_s("xstep.cascade.unit", "MM")
    reader = IGESCAFControl_Reader()
    reader.SetNameMode(True)
    reader.SetColorMode(True)
    reader.SetLayerMode(True)
    status = reader.ReadFile(str(path))
    if status != IFSelect_RetDone:
        raise StepReadError(f"OCCT failed to read IGES file ({status}): {path}")
    if not reader.Transfer(document):
        raise StepReadError(f"OCCT failed to transfer IGES data into XDE: {path}")
    header = inspect_iges_header(path)
    unit = header.length_unit
    return (unit,) if unit else ()


class IgesImporter:
    """Importer for IGES files through the same XDE mapping as STEP."""

    format_name = "iges"
    suffixes = frozenset({".igs", ".iges"})

    def __init__(self, *, body_mesh_resources: bool = True) -> None:
        self.body_mesh_resources = body_mesh_resources

    def probe(self, path: Path) -> float:
        """Return a bounded confidence from the suffix and section flags."""
        try:
            header = inspect_iges_header(path)
        except OSError:
            return 0.0
        score = 0.0
        if path.suffix.lower() in self.suffixes:
            score += 0.3
        if header.is_iges:
            score += 0.7
        return min(score, 1.0)

    def import_file(
        self,
        path: Path,
        *,
        options: ImportOptions,
        resources: ResourceWriter,
    ) -> ImportResult:
        """Read one IGES file and map its resolved XDE graph to common IR."""
        source_path = Path(path)
        if options.ir_version != CURRENT_IR_VERSION:
            raise UnsupportedIRVersionError(
                f"cad3d-ir-step supports IR {CURRENT_IR_VERSION}, "
                f"not {options.ir_version}"
            )
        if not source_path.is_file():
            raise StepReadError(f"IGES source is not a file: {source_path}")
        header = inspect_iges_header(source_path)
        if not header.is_iges:
            raise StepReadError(f"not an IGES file: {source_path}")

        source_digest, source_size = hash_file(source_path)
        source_id = f"source-iges-{source_digest[:12]}"
        source = SourceDocument(
            id=source_id,
            name=source_path.name,
            format="iges",
            uri=source_path.name,
            sha256=source_digest,
            properties={
                "iges_unit_flag": header.unit_flag,
                "iges_product": header.product,
            },
        )
        diagnostics: list[ImportDiagnostic] = []
        try:
            xde = read_xde(source_path, transfer=_transfer_iges)
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
                    prefix="IGES",
                    resource_properties={},
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
                source_id=source_id, counts=xde.unsupported_counts, prefix="IGES"
            )
        )
        diagnostics.extend(
            unit_diagnostics(
                source_id=source_id,
                source_length_units=xde.source_length_units,
                prefix="IGES",
            )
        )
        if options.validate:
            validate_package(package)
        statistics: dict[str, Any] = {
            "source_byte_length": source_size,
            "source_length_units": list(xde.source_length_units),
            **package_statistics(package),
        }
        return ImportResult(
            package=package, diagnostics=diagnostics, statistics=statistics
        )


__all__ = ["IgesHeader", "IgesImporter", "inspect_iges_header"]
