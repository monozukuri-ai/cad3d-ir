"""Write a Parasolid model as STEP from its converted exact bodies.

The solids are the file's own B-Rep bodies converted by parasolid-kit, placed
by the assembly instances the file stores. The STEP file is a flat assembly:
one component per placed body, named after the body's ``SDL/TYSA_NAME``
attribute when it has one, with the body and face colours of the
``SDL/TYSA_COLOUR`` attributes.
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
    _rgba,
    _validate,
    _write_document,
    quiet_occt,
)

from cad3d_ir_parasolid.errors import ParasolidExportError
from cad3d_ir_parasolid.importer import (
    BodyRecord,
    ParasolidLimits,
    ParasolidSource,
    classify,
    placements,
    read_source,
)
from cad3d_ir_parasolid.shapes import location_from_matrix

_DEFAULT_NAME = "model"


@dataclass(frozen=True, slots=True)
class ParasolidStepResult:
    """What :func:`export_step` wrote."""

    path: Path
    byte_length: int
    schema: StepSchema
    unit: StepUnit
    schema_key: str | None
    bodies: int
    placed: int
    coloured_bodies: int
    coloured_faces: int
    skipped: tuple[tuple[str, str], ...]


def _set_name(label: Any, name: str) -> None:
    """Name a label from a Python string (UTF-8, so non-ASCII names survive)."""
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.TDataStd import TDataStd_Name

    TDataStd_Name.Set_s(label, TCollection_ExtendedString(name, True))


def _colour_label(
    shape_tool: Any, color_tool: Any, label: Any, body: BodyRecord
) -> int:
    """Set the body colour and the face colours on a referred shape label."""
    from OCP.XCAFDoc import XCAFDoc_ColorSurf

    coloured = 0
    if body.colour is not None:
        color_tool.SetColor(label, _rgba(body.colour), XCAFDoc_ColorSurf)
    for face, colour in body.face_shapes:
        sub_label = shape_tool.AddSubShape(label, face)
        if sub_label.IsNull():
            continue
        color_tool.SetColor(sub_label, _rgba(colour), XCAFDoc_ColorSurf)
        coloured += 1
    return coloured


def export_step(
    src: Path | str | ParasolidSource,
    dst: Path | str,
    *,
    schema: StepSchema = "AP214",
    unit: StepUnit = "mm",
    name: str | None = None,
    limits: ParasolidLimits | None = None,
) -> ParasolidStepResult:
    """Write the converted bodies of a transmit file as a STEP assembly.

    ``src`` may be a path or a :class:`ParasolidSource` read with conversion.
    The destination must not exist. Bodies that did not convert are listed in
    ``skipped`` with their reason.
    """
    _validate(schema, unit)
    target = Path(dst)
    if target.exists():
        raise ParasolidExportError(f"destination already exists: {target}")
    source = (
        src if isinstance(src, ParasolidSource) else read_source(src, limits=limits)
    )
    if source.parse_error is not None:
        raise ParasolidExportError(
            f"parasolid-kit could not read {source.path}: "
            f"{source.parse_error.code}: {source.parse_error.message}"
        )
    if source.conversion is None:
        failure = source.conversion_error
        detail = "" if failure is None else f": {failure.code}: {failure.message}"
        raise ParasolidExportError(
            f"the bodies of {source.path} were not converted to OCCT{detail}"
        )
    classification = classify(source)
    skipped = tuple(
        (body.label, body.reason or body.status)
        for body in classification.bodies
        if body.shape is None
    )
    placed = placements(source.parsed.brep, classification)
    if not placed:
        raise ParasolidExportError(
            f"no body of {source.path} yielded a shape to write as STEP"
        )

    from OCP.BRep import BRep_Builder
    from OCP.TDF import TDF_Label, TDF_LabelSequence
    from OCP.TopoDS import TopoDS_Compound
    from OCP.XCAFDoc import XCAFDoc_DocumentTool

    document = _new_document()
    shape_tool = XCAFDoc_DocumentTool.ShapeTool_s(document.Main())
    color_tool = XCAFDoc_DocumentTool.ColorTool_s(document.Main())
    coloured_faces = 0
    coloured_bodies = 0
    with quiet_occt():
        builder = BRep_Builder()
        compound = TopoDS_Compound()
        builder.MakeCompound(compound)
        for item in placed:
            builder.Add(
                compound, item.body.shape.Moved(location_from_matrix(item.matrix))
            )
        root = shape_tool.AddShape(compound, True)
        _set_name(root, (name or source.path.stem or _DEFAULT_NAME).strip())
        components = TDF_LabelSequence()
        shape_tool.GetComponents_s(root, components)
        counts: dict[str, int] = {}
        coloured_labels: set[int] = set()
        for position in range(1, min(components.Length(), len(placed)) + 1):
            item = placed[position - 1]
            component = components.Value(position)
            referred = TDF_Label()
            body_name = item.body.label
            if shape_tool.GetReferredShape_s(component, referred):
                _set_name(referred, body_name)
                if item.body.id not in coloured_labels:
                    coloured_labels.add(item.body.id)
                    coloured_faces += _colour_label(
                        shape_tool, color_tool, referred, item.body
                    )
                    if item.body.colour is not None:
                        coloured_bodies += 1
            counts[body_name] = counts.get(body_name, 0) + 1
            _set_name(component, f"{body_name}:{counts[body_name]}")
    byte_length = _write_document(
        document, target, schema=schema, unit=unit, tessellated=False, colors=True
    )
    return ParasolidStepResult(
        path=target,
        byte_length=byte_length,
        schema=schema,
        unit=unit,
        schema_key=source.schema_key,
        bodies=len(classification.converted),
        placed=len(placed),
        coloured_bodies=coloured_bodies,
        coloured_faces=coloured_faces,
        skipped=skipped,
    )


__all__ = ["STEP_SCHEMAS", "STEP_UNITS", "ParasolidStepResult", "export_step"]
