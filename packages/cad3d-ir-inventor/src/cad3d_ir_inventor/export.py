"""Write an Inventor part as STEP from its converted exact bodies.

The solids are the bodies of the stored B-Rep table as inventor-kit converts
them. The STEP file is a flat assembly: one component per converted body,
named ``Body_<index>`` after the body's place in the table. Appearances are
not read, so the file carries no colours.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cad3d_ir_step.export import (
    STEP_SCHEMAS,
    STEP_UNITS,
    StepSchema,
    StepUnit,
    _new_document,
    _validate,
    _write_document,
    quiet_occt,
)

from cad3d_ir_inventor.errors import InventorExportError
from cad3d_ir_inventor.importer import InventorSource, classify, read_source

_DEFAULT_NAME = "model"


@dataclass(frozen=True, slots=True)
class InventorStepResult:
    """What :func:`export_step` wrote."""

    path: Path
    byte_length: int
    schema: StepSchema
    unit: StepUnit
    major: int | None
    bodies: int
    approximated_bodies: int
    skipped: tuple[tuple[str, str], ...]


def _set_name(label: Any, name: str) -> None:
    """Name a label from a Python string (UTF-8, so non-ASCII names survive)."""
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.TDataStd import TDataStd_Name

    TDataStd_Name.Set_s(label, TCollection_ExtendedString(name, True))


def export_step(
    src: Path | str | InventorSource,
    dst: Path | str,
    *,
    schema: StepSchema = "AP214",
    unit: StepUnit = "mm",
    name: str | None = None,
    limits: Any | None = None,
) -> InventorStepResult:
    """Write the converted bodies of a part as a STEP assembly.

    ``src`` may be a path or an :class:`InventorSource` read with conversion.
    The destination must not exist. Bodies that did not convert are listed in
    ``skipped`` with their reason.
    """
    _validate(schema, unit)
    target = Path(dst)
    if target.exists():
        raise InventorExportError(f"destination already exists: {target}")
    source = src if isinstance(src, InventorSource) else read_source(src, limits=limits)
    if source.read_error is not None:
        raise InventorExportError(
            f"inventor-kit could not read {source.path}: {source.read_error.message}"
        )
    if source.conversion is None:
        failure = source.conversion_error
        if failure is not None:
            detail = f": {failure.message}"
        elif source.geometry_reasons:
            detail = f": {'; '.join(source.geometry_reasons[:3])}"
        else:
            detail = ""
        raise InventorExportError(
            f"the bodies of {source.path} were not converted to OCCT{detail}"
        )
    classification = classify(source)
    converted = classification.converted
    skipped = tuple(
        (body.label, body.reason or body.status)
        for body in classification.bodies
        if body.shape is None
    )
    if not converted:
        raise InventorExportError(
            f"no body of {source.path} yielded a shape to write as STEP"
        )

    from OCP.BRep import BRep_Builder
    from OCP.TDF import TDF_Label, TDF_LabelSequence
    from OCP.TopoDS import TopoDS_Compound
    from OCP.XCAFDoc import XCAFDoc_DocumentTool

    document = _new_document()
    shape_tool = XCAFDoc_DocumentTool.ShapeTool_s(document.Main())
    with quiet_occt():
        builder = BRep_Builder()
        compound = TopoDS_Compound()
        builder.MakeCompound(compound)
        for body in converted:
            builder.Add(compound, body.shape)
        root = shape_tool.AddShape(compound, True)
        _set_name(root, (name or source.path.stem or _DEFAULT_NAME).strip())
        components = TDF_LabelSequence()
        shape_tool.GetComponents_s(root, components)
        for position in range(1, min(components.Length(), len(converted)) + 1):
            body = converted[position - 1]
            component = components.Value(position)
            referred = TDF_Label()
            if shape_tool.GetReferredShape_s(component, referred):
                _set_name(referred, body.label)
            _set_name(component, f"{body.label}:1")
    byte_length = _write_document(
        document, target, schema=schema, unit=unit, tessellated=False, colors=False
    )
    return InventorStepResult(
        path=target,
        byte_length=byte_length,
        schema=schema,
        unit=unit,
        major=source.major,
        bodies=len(converted),
        approximated_bodies=sum(not body.exact for body in converted),
        skipped=skipped,
    )


__all__ = ["STEP_SCHEMAS", "STEP_UNITS", "InventorStepResult", "export_step"]
