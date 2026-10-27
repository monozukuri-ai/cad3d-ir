"""Write an ICD model as STEP from icadkit's exact part solids.

Unlike the mesh route of ``cad3d_ir_step.mesh_to_step``, the solids here are
the B-Rep bodies iCAD SX saved (converted by icadkit through parasolid-kit),
placed by the saved part frames. The STEP file is a flat assembly: one
component per solid body, named after its part; parts whose solids are
identical share one referred product.
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

from cad3d_ir_icad.errors import IcadExportError
from cad3d_ir_icad.importer import IcadLimits, IcadSource, classify, read_source
from cad3d_ir_icad.shapes import location_from_matrix

_DEFAULT_NAME = "model"


@dataclass(frozen=True, slots=True)
class IcadStepResult:
    """What :func:`export_step` wrote."""

    path: Path
    byte_length: int
    schema: StepSchema
    unit: StepUnit
    version_label: str | None
    parts: int
    solids: int
    bodies: int
    shared_definitions: int
    skipped: tuple[tuple[str, str], ...]


def _set_name(label: Any, name: str) -> None:
    """Name a label from a Python string (UTF-8, so CP932 part names survive)."""
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.TDataStd import TDataStd_Name

    TDataStd_Name.Set_s(label, TCollection_ExtendedString(name, True))


def _bodies_of(shape: Any) -> list[Any]:
    """The solids inside a part compound (the shape itself when not a compound)."""
    from OCP.TopAbs import TopAbs_COMPOUND
    from OCP.TopoDS import TopoDS_Iterator

    if shape.ShapeType() != TopAbs_COMPOUND:
        return [shape]
    items = []
    iterator = TopoDS_Iterator(shape)
    while iterator.More():
        items.append(iterator.Value())
        iterator.Next()
    return items


def export_step(
    src: Path | str | IcadSource,
    dst: Path | str,
    *,
    schema: StepSchema = "AP214",
    unit: StepUnit = "mm",
    name: str | None = None,
    limits: IcadLimits | None = None,
) -> IcadStepResult:
    """Write the saved solids of an ICD file as a STEP assembly.

    ``src`` may be a path or an :class:`IcadSource` read with shapes. The
    destination must not exist. Parts without a convertible solid are listed
    in ``skipped`` with their reason (external references, unsupported bodies).
    """
    _validate(schema, unit)
    target = Path(dst)
    if target.exists():
        raise IcadExportError(f"destination already exists: {target}")
    source = src if isinstance(src, IcadSource) else read_source(src, limits=limits)
    if source.parts_unqualified and source.views_unqualified:
        raise IcadExportError(
            f"iCAD SX version {source.version_label or 'of unknown layout'} "
            "has not been qualified by icadkit; nothing can be written"
        )
    classification = classify(source)
    skipped: list[tuple[str, str]] = []
    shapes_by_part: dict[str, Any] = {}
    if source.shapes is not None:
        shapes_by_part = {shape.part_id: shape for shape in source.shapes.shapes}
    for part in source.part_list:
        if part.part_id in classification.solids:
            continue
        if part.part_id in classification.external:
            skipped.append((part.name or part.part_id, "external reference"))
            continue
        shape = shapes_by_part.get(part.part_id)
        if shape is not None and shape.reason not in (None, "shape.no_bodies"):
            skipped.append((part.name or part.part_id, str(shape.reason)))
    solids = list(classification.solids.values())
    if not solids:
        raise IcadExportError(
            f"no part of {source.path} yielded a solid to write as STEP"
        )

    from OCP.BRep import BRep_Builder
    from OCP.TDF import TDF_Label, TDF_LabelSequence
    from OCP.TopoDS import TopoDS_Compound
    from OCP.XCAFDoc import XCAFDoc_DocumentTool

    base_by_key: dict[str, Any] = {}
    placed: list[Any] = []
    document = _new_document()
    shape_tool = XCAFDoc_DocumentTool.ShapeTool_s(document.Main())
    with quiet_occt():
        builder = BRep_Builder()
        compound = TopoDS_Compound()
        builder.MakeCompound(compound)
        for solid in solids:
            # Parts with identical solids share the base shape, so the writer
            # emits one referred product with several placed components.
            base = base_by_key.setdefault(solid.key, solid.shape)
            location = location_from_matrix(solid.world_transform)
            for body in _bodies_of(base):
                builder.Add(compound, body.Moved(location))
                placed.append(solid)
        root = shape_tool.AddShape(compound, True)
        _set_name(root, (name or source.path.stem or _DEFAULT_NAME).strip())
        components = TDF_LabelSequence()
        shape_tool.GetComponents_s(root, components)
        counts: dict[str, int] = {}
        for position in range(1, min(components.Length(), len(placed)) + 1):
            solid = placed[position - 1]
            component = components.Value(position)
            referred = TDF_Label()
            part_name = solid.name or f"part-{position}"
            if shape_tool.GetReferredShape_s(component, referred):
                _set_name(referred, part_name)
            counts[part_name] = counts.get(part_name, 0) + 1
            _set_name(component, f"{part_name}:{counts[part_name]}")
    byte_length = _write_document(
        document, target, schema=schema, unit=unit, tessellated=False, colors=False
    )
    return IcadStepResult(
        path=target,
        byte_length=byte_length,
        schema=schema,
        unit=unit,
        version_label=source.version_label,
        parts=len(source.part_list),
        solids=len(solids),
        bodies=len(placed),
        shared_definitions=len(base_by_key),
        skipped=tuple(skipped),
    )


__all__ = ["STEP_SCHEMAS", "STEP_UNITS", "IcadStepResult", "export_step"]
