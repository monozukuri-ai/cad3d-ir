"""Write STEP: re-export a STEP / IGES document, or build faceted STEP from meshes.

Two entry points share one writer:

* :func:`export_step` reads a STEP or IGES file into XCAF again and writes it
  with ``STEPCAFControl_Writer`` in the requested schema (AP203 / AP214 /
  AP242) and unit. Definition colours are copied onto every component label
  first: the writer otherwise loses the surface colour of instanced shapes
  (on an assembly of twelve coloured parts only four kept their colour).
* :func:`mesh_to_step` turns the triangles of a visualization GLB, or any
  :class:`MeshBody`, into one solid per body (``BRepBuilderAPI_MakeShapeOnMesh``
  → ``BRepBuilderAPI_Sewing`` → ``BRepBuilderAPI_MakeSolid``). Every face is a
  planar triangle, so the file opens in any CAD but is not editable geometry,
  and it is large (about 2.4 KB per triangle). Sewing time grows with the
  square of the triangle count (14k triangles in 2 s, 100k in 235 s), so a
  cap is enforced before any geometry is built. ``mode="tessellated"`` writes
  AP242 ``TRIANGULATED_FACE`` entities instead: small and fast, but not every
  CAD reads them.

OCCT's writer reports its progress on the process stdout; both functions
lower the messenger trace level to warnings while they write.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal

from cad3d_ir import ImportDiagnostic
from cad3d_ir.mesh import GlbReadError, MeshScene, read_glb
from OCP.BRep import BRep_Builder, BRep_Tool
from OCP.BRepBuilderAPI import (
    BRepBuilderAPI_MakeShapeOnMesh,
    BRepBuilderAPI_MakeSolid,
    BRepBuilderAPI_Sewing,
)
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepGProp import BRepGProp
from OCP.BRepLib import BRepLib
from OCP.gp import gp_Pnt
from OCP.GProp import GProp_GProps
from OCP.IFSelect import IFSelect_RetDone
from OCP.Interface import Interface_Static
from OCP.Message import Message, Message_Gravity, Message_Warning
from OCP.Poly import Poly_Triangle, Poly_Triangulation
from OCP.Quantity import Quantity_Color, Quantity_ColorRGBA, Quantity_TOC_RGB
from OCP.Standard import Standard_Failure
from OCP.STEPCAFControl import STEPCAFControl_Writer
from OCP.STEPControl import STEPControl_AsIs, STEPControl_Controller
from OCP.TCollection import TCollection_ExtendedString
from OCP.TDataStd import TDataStd_Name
from OCP.TDF import TDF_Label, TDF_LabelSequence
from OCP.TDocStd import TDocStd_Document
from OCP.TopAbs import TopAbs_FACE, TopAbs_SHELL, TopAbs_SOLID
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS, TopoDS_Compound, TopoDS_Face, TopoDS_Shell
from OCP.XCAFApp import XCAFApp_Application
from OCP.XCAFDoc import XCAFDoc_ColorSurf, XCAFDoc_DocumentTool

from cad3d_ir_step.errors import MeshTooLargeError, StepExportError
from cad3d_ir_step.header import inspect_step_header
from cad3d_ir_step.iges import IgesImporter, _transfer_iges
from cad3d_ir_step.importer import _transfer_step
from cad3d_ir_step.xde import XdeReadResult, read_xde

StepSchema = Literal["AP203", "AP214", "AP242"]
StepUnit = Literal["mm", "cm", "m", "inch", "ft"]
MeshStepMode = Literal["solid", "tessellated"]
SourceFormat = Literal["step", "iges"]

STEP_SCHEMAS: Final[tuple[StepSchema, ...]] = ("AP203", "AP214", "AP242")
STEP_UNITS: Final[tuple[StepUnit, ...]] = ("mm", "cm", "m", "inch", "ft")
DEFAULT_MAX_TRIANGLES: Final = 20_000
"""Sewn-solid cap: about 5 s of sewing, 50 MB of STEP and under 1 GB of memory."""

_SCHEMA_STATICS: Final = {"AP203": "AP203", "AP214": "AP214IS", "AP242": "AP242DIS"}
_UNIT_STATICS: Final = {"mm": "MM", "cm": "CM", "m": "M", "inch": "INCH", "ft": "FT"}
_PMI_COUNTERS: Final = (
    "dimensions",
    "geometric_tolerances",
    "datums",
    "legacy_dimensional_tolerances",
)
_DEFAULT_BODY_NAME: Final = "body"
_DEFAULT_ASSEMBLY_NAME: Final = "assembly"
Rgba = tuple[float, float, float, float]


@dataclass(frozen=True, slots=True)
class MeshBody:
    """One body's triangles: flat ``x, y, z`` in millimetres, three indices per face."""

    name: str
    positions: Sequence[float]
    indices: Sequence[int]
    color: Rgba | None = None


@dataclass(frozen=True, slots=True)
class StepExportResult:
    """What :func:`export_step` wrote and what the source document held."""

    path: Path
    byte_length: int
    schema: StepSchema
    unit: StepUnit
    source_format: SourceFormat
    simple_shapes: int
    assemblies: int
    components: int
    colored_shapes: int
    colors_copied: int
    colored_shapes_after: int | None
    source_length_units: tuple[str, ...]
    diagnostics: list[ImportDiagnostic] = field(default_factory=list)
    faces: int = 0
    """Faces of the source document's simple shapes."""
    tessellated_faces: int = 0
    """Of those, faces without a surface (AP242 tessellated geometry). They are
    written only when ``schema`` is AP242."""

    @property
    def products(self) -> int:
        return self.simple_shapes + self.assemblies


@dataclass(frozen=True, slots=True)
class MeshStepResult:
    """What :func:`mesh_to_step` built and wrote."""

    path: Path
    byte_length: int
    schema: StepSchema
    mode: MeshStepMode
    bodies: int
    triangles: int
    vertices: int
    degenerate_triangles: int
    solids: int
    open_shells: int
    free_edges: int
    volume_mm3: float | None
    names: tuple[str, ...]
    diagnostics: list[ImportDiagnostic] = field(default_factory=list)


# -- OCCT plumbing -------------------------------------------------------------


@contextmanager
def quiet_occt(level: Message_Gravity = Message_Warning) -> Iterator[None]:
    """Lower the OCCT messenger trace level while writing (restored afterwards)."""
    printers = Message.DefaultMessenger_s().Printers()
    previous = []
    for index in range(1, printers.Length() + 1):
        printer = printers.Value(index)
        previous.append((printer, printer.GetTraceLevel()))
        printer.SetTraceLevel(level)
    try:
        yield
    finally:
        for printer, trace in previous:
            printer.SetTraceLevel(trace)


def _set_static(name: str, value: str) -> None:
    if not Interface_Static.SetCVal_s(name, value):
        raise StepExportError(f"OCCT rejected {name}={value!r}")


def _write_document(
    document: Any,
    dst: Path,
    *,
    schema: StepSchema,
    unit: StepUnit,
    tessellated: bool = False,
    colors: bool = True,
) -> int:
    """Write an XCAF document as STEP and return the file size."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    STEPControl_Controller.Init_s()
    _set_static("write.step.schema", _SCHEMA_STATICS[schema])
    _set_static("write.step.unit", _UNIT_STATICS[unit])
    # Only AP242 has tessellated entities: faces without a surface are written
    # as TRIANGULATED_FACE there and left out of AP203 / AP214 (OCCT would
    # otherwise put the AP242 entities into those files).
    if tessellated:
        mode = "On"
    elif schema == "AP242":
        mode = "OnNoBRep"
    else:
        mode = "Off"
    _set_static("write.step.tessellated", mode)
    try:
        with quiet_occt():
            writer = STEPCAFControl_Writer()
            writer.SetColorMode(colors)
            writer.SetNameMode(True)
            writer.SetLayerMode(True)
            writer.SetMaterialMode(True)
            try:
                transferred = writer.Transfer(document, STEPControl_AsIs)
                status = writer.Write(str(dst)) if transferred else None
            except Standard_Failure as exc:
                raise StepExportError(
                    f"OCCT failed while writing {dst}: {exc}"
                ) from exc
    finally:
        Interface_Static.SetCVal_s("write.step.tessellated", "OnNoBRep")
    if not transferred:
        raise StepExportError(f"OCCT could not transfer the document to STEP: {dst}")
    if status != IFSelect_RetDone:
        raise StepExportError(f"OCCT failed to write the STEP file ({status}): {dst}")
    return dst.stat().st_size


def _validate(schema: str, unit: str) -> None:
    if schema not in _SCHEMA_STATICS:
        raise ValueError(f"schema must be one of {STEP_SCHEMAS}, not {schema!r}")
    if unit not in _UNIT_STATICS:
        raise ValueError(f"unit must be one of {STEP_UNITS}, not {unit!r}")


def _new_document() -> Any:
    application = XCAFApp_Application.GetApplication_s()
    document = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
    application.InitDocument(document)
    return document


def _set_name(label: Any, name: str) -> None:
    TDataStd_Name.Set_s(label, TCollection_ExtendedString(name))


def _rgba(color: Rgba) -> Any:
    red, green, blue, alpha = (min(max(float(c), 0.0), 1.0) for c in color)
    return Quantity_ColorRGBA(Quantity_Color(red, green, blue, Quantity_TOC_RGB), alpha)


# -- STEP / IGES re-export -----------------------------------------------------


@dataclass(slots=True)
class _LabelStats:
    simple_shapes: int = 0
    assemblies: int = 0
    components: int = 0
    colored: int = 0
    faces: int = 0
    tessellated_faces: int = 0


def _label_stats(document: Any) -> _LabelStats:
    shape_tool = XCAFDoc_DocumentTool.ShapeTool_s(document.Main())
    color_tool = XCAFDoc_DocumentTool.ColorTool_s(document.Main())
    labels = TDF_LabelSequence()
    shape_tool.GetShapes(labels)
    stats = _LabelStats()
    for index in range(1, labels.Length() + 1):
        label = labels.Value(index)
        if shape_tool.IsAssembly_s(label):
            stats.assemblies += 1
            components = TDF_LabelSequence()
            shape_tool.GetComponents_s(label, components)
            stats.components += components.Length()
        elif shape_tool.IsSimpleShape_s(label):
            stats.simple_shapes += 1
            explorer = TopExp_Explorer(shape_tool.GetShape_s(label), TopAbs_FACE)
            while explorer.More():
                stats.faces += 1
                if BRep_Tool.Surface_s(TopoDS.Face_s(explorer.Current())) is None:
                    stats.tessellated_faces += 1
                explorer.Next()
        if color_tool.IsSet(label, XCAFDoc_ColorSurf):
            stats.colored += 1
    return stats


def _copy_definition_colors(document: Any) -> int:
    """Give every component label its definition's surface colour; return the count.

    ``STEPCAFControl_Writer`` writes the styled items of instanced shapes
    unreliably (observed: 12 coloured simple shapes, 4 kept after a round
    trip). A colour on the component label itself survives. Components that
    already carry their own colour are left alone.
    """
    shape_tool = XCAFDoc_DocumentTool.ShapeTool_s(document.Main())
    color_tool = XCAFDoc_DocumentTool.ColorTool_s(document.Main())
    labels = TDF_LabelSequence()
    shape_tool.GetShapes(labels)
    copied = 0
    for index in range(1, labels.Length() + 1):
        label = labels.Value(index)
        if not shape_tool.IsAssembly_s(label):
            continue
        components = TDF_LabelSequence()
        shape_tool.GetComponents_s(label, components)
        for position in range(1, components.Length() + 1):
            component = components.Value(position)
            if color_tool.IsSet(component, XCAFDoc_ColorSurf):
                continue
            referred = TDF_Label()
            color = Quantity_ColorRGBA()
            if shape_tool.GetReferredShape_s(
                component, referred
            ) and color_tool.GetColor_s(referred, XCAFDoc_ColorSurf, color):
                color_tool.SetColor(component, color, XCAFDoc_ColorSurf)
                copied += 1
    return copied


def source_format_of(path: Path) -> SourceFormat:
    """Detect ``"step"`` or ``"iges"``; raise ``StepExportError`` for other files."""
    path = Path(path)
    if not path.is_file():
        raise StepExportError(f"source is not a file: {path}")
    if inspect_step_header(path).is_part21:
        return "step"
    if IgesImporter().probe(path) > 0.0:
        return "iges"
    raise StepExportError(f"not a STEP Part 21 or IGES file: {path}")


def _read(path: Path, source_format: SourceFormat) -> XdeReadResult:
    transfer = _transfer_step if source_format == "step" else _transfer_iges
    try:
        with quiet_occt():
            return read_xde(path, transfer=transfer)
    except Standard_Failure as exc:
        raise StepExportError(f"OCCT failed while reading {path}: {exc}") from exc


def export_step(
    src: Path | str,
    dst: Path | str,
    *,
    schema: StepSchema = "AP214",
    unit: StepUnit = "mm",
    keep_colors: bool = True,
    verify: bool = False,
) -> StepExportResult:
    """Re-export a STEP or IGES file as STEP in ``schema`` and ``unit``.

    The document is read into XCAF again (the importers close their document
    after mapping), so this costs one more read of the source. ``keep_colors``
    copies definition colours onto component labels before writing (see
    :func:`_copy_definition_colors`). ``verify`` reads the written file back
    and reports fewer coloured shapes as ``STEP_COLORS_CHANGED``; it costs a
    third read, so callers decide by file size.
    """
    _validate(schema, unit)
    source = Path(src)
    target = Path(dst)
    source_format = source_format_of(source)
    xde = _read(source, source_format)
    try:
        before = _label_stats(xde.document)
        copied = _copy_definition_colors(xde.document) if keep_colors else 0
        byte_length = _write_document(xde.document, target, schema=schema, unit=unit)
        unsupported = dict(xde.unsupported_counts)
        source_units = tuple(xde.source_length_units)
    finally:
        xde.close()

    diagnostics = [
        ImportDiagnostic(
            code="STEP_EXPORT_WRITTEN",
            severity="info",
            action="preserved",
            message=(
                f"Wrote STEP {schema} in {unit} from the {source_format.upper()} "
                "document: product structure, placements, names and colours."
            ),
            details={
                "schema": schema,
                "unit": unit,
                "source_format": source_format,
                "simple_shapes": before.simple_shapes,
                "assemblies": before.assemblies,
                "components": before.components,
            },
        )
    ]
    if copied:
        diagnostics.append(
            ImportDiagnostic(
                code="STEP_COLORS_COPIED",
                severity="info",
                action="preserved",
                message=(
                    f"Copied the definition colour onto {copied} component(s) so "
                    "instanced shapes keep their colour in the written STEP."
                ),
                details={"components": copied},
            )
        )
    if before.tessellated_faces and schema == "AP242":
        diagnostics.append(
            ImportDiagnostic(
                code="STEP_TESSELLATED_WRITTEN",
                severity="info",
                action="preserved",
                message=(
                    f"{before.tessellated_faces} face(s) have no B-rep surface and "
                    "were written as AP242 tessellated geometry (TRIANGULATED_FACE)."
                ),
                details={"faces": before.tessellated_faces, "all_faces": before.faces},
            )
        )
    elif before.tessellated_faces:
        diagnostics.append(
            ImportDiagnostic(
                code="STEP_TESSELLATED_NOT_WRITTEN",
                severity="warning",
                action="skipped",
                message=(
                    f"{before.tessellated_faces} face(s) carry only tessellated "
                    f"geometry, which {schema} cannot represent; they were left out. "
                    "Write AP242 to keep them."
                ),
                details={
                    "faces": before.tessellated_faces,
                    "all_faces": before.faces,
                    "schema": schema,
                },
            )
        )
    pmi = {name: unsupported.get(name, 0) for name in _PMI_COUNTERS}
    if any(pmi.values()):
        diagnostics.append(
            ImportDiagnostic(
                code="STEP_PMI_NOT_WRITTEN",
                severity="warning",
                action="skipped",
                message=(
                    "Dimensions, tolerances and datums (PMI) of the source were "
                    "not written to the STEP output."
                ),
                details=pmi,
            )
        )
    after: int | None = None
    if verify:
        check = _read(target, "step")
        try:
            after = _label_stats(check.document).colored
        finally:
            check.close()
        if after < before.colored:
            diagnostics.append(
                ImportDiagnostic(
                    code="STEP_COLORS_CHANGED",
                    severity="warning",
                    action="approximated",
                    message=(
                        f"The written STEP carries colour on {after} shape(s) "
                        f"where the source had {before.colored}."
                    ),
                    details={"before": before.colored, "after": after},
                )
            )
    return StepExportResult(
        path=target,
        byte_length=byte_length,
        schema=schema,
        unit=unit,
        source_format=source_format,
        simple_shapes=before.simple_shapes,
        assemblies=before.assemblies,
        components=before.components,
        colored_shapes=before.colored,
        colors_copied=copied,
        colored_shapes_after=after,
        source_length_units=source_units,
        diagnostics=diagnostics,
        faces=before.faces,
        tessellated_faces=before.tessellated_faces,
    )


# -- mesh → STEP -----------------------------------------------------------------


def _material_color(scene: MeshScene, material: int | None) -> Rgba | None:
    if material is None or not 0 <= material < len(scene.materials):
        return None
    pbr = scene.materials[material].get("pbrMetallicRoughness")
    factor = pbr.get("baseColorFactor") if isinstance(pbr, dict) else None
    if isinstance(factor, list) and len(factor) == 4:
        return (float(factor[0]), float(factor[1]), float(factor[2]), float(factor[3]))
    return None


def bodies_from_scene(scene: MeshScene) -> tuple[list[MeshBody], int]:
    """One :class:`MeshBody` per placed vertex set, in millimetres.

    Returns the bodies and how many of them merged several primitive colours
    into one (a body gets its first primitive's colour).
    """
    factor = scene.length_unit_to_m * 1000.0
    bodies: list[MeshBody] = []
    merged = 0
    used_names: dict[str, int] = {}
    for placed in scene.placed_vertex_sets(lines=False):
        indices: list[int] = []
        colors: list[Rgba | None] = []
        for primitive in placed.triangles:
            indices.extend(primitive.indices)
            colors.append(_material_color(scene, primitive.material))
        distinct = {color for color in colors if color is not None}
        if len(distinct) > 1:
            merged += 1
        color = next((color for color in colors if color is not None), None)
        positions: Sequence[float] = placed.positions
        if factor != 1.0:
            positions = [value * factor for value in positions]
        base = (placed.node.name or "").strip() or _DEFAULT_BODY_NAME
        seen = used_names.get(base, 0)
        used_names[base] = seen + 1
        name = base if seen == 0 else f"{base}-{seen + 1}"
        bodies.append(
            MeshBody(name=name, positions=positions, indices=indices, color=color)
        )
    return bodies, merged


@dataclass(slots=True)
class _Welded:
    points: list[tuple[float, float, float]]
    faces: list[tuple[int, int, int]]
    degenerate: int


def _weld(body: MeshBody) -> _Welded:
    """Share identical vertices and drop zero-area triangles."""
    positions = body.positions
    if len(positions) % 3:
        raise StepExportError(f"body {body.name!r}: positions are not 3D points")
    if len(body.indices) % 3:
        raise StepExportError(f"body {body.name!r}: indices do not come in threes")
    lookup: dict[tuple[float, float, float], int] = {}
    points: list[tuple[float, float, float]] = []
    remap: list[int] = []
    for start in range(0, len(positions), 3):
        point = (
            float(positions[start]),
            float(positions[start + 1]),
            float(positions[start + 2]),
        )
        index = lookup.get(point)
        if index is None:
            index = len(points)
            lookup[point] = index
            points.append(point)
        remap.append(index)
    faces: list[tuple[int, int, int]] = []
    degenerate = 0
    count = len(remap)
    indices = body.indices
    for start in range(0, len(indices), 3):
        a, b, c = int(indices[start]), int(indices[start + 1]), int(indices[start + 2])
        if not (0 <= a < count and 0 <= b < count and 0 <= c < count):
            raise StepExportError(
                f"body {body.name!r}: triangle indices exceed the vertices"
            )
        a, b, c = remap[a], remap[b], remap[c]
        if a in (b, c) or b == c:
            degenerate += 1
            continue
        (ax, ay, az), (bx, by, bz), (cx, cy, cz) = points[a], points[b], points[c]
        ux, uy, uz = bx - ax, by - ay, bz - az
        vx, vy, vz = cx - ax, cy - ay, cz - az
        if (uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx) == (0.0, 0.0, 0.0):
            degenerate += 1
            continue
        faces.append((a, b, c))
    return _Welded(points=points, faces=faces, degenerate=degenerate)


def _triangulation(welded: _Welded) -> Any:
    triangulation = Poly_Triangulation(
        len(welded.points), len(welded.faces), False, False
    )
    for index, (x, y, z) in enumerate(welded.points, start=1):
        triangulation.SetNode(index, gp_Pnt(x, y, z))
    for index, (a, b, c) in enumerate(welded.faces, start=1):
        triangulation.SetTriangle(index, Poly_Triangle(a + 1, b + 1, c + 1))
    return triangulation


@dataclass(slots=True)
class _BuiltBody:
    body: MeshBody
    shape: Any
    solids: int
    open_shells: int
    free_edges: int


def _iter_shells(shape: Any) -> Iterator[Any]:
    """Yield the shells of a sewn shape; faces outside any shell become one shell.

    Sewing returns a single face as itself and may leave unconnected faces
    beside the shells of a compound.
    """
    if shape.ShapeType() == TopAbs_SHELL:
        yield TopoDS.Shell_s(shape)
        return
    explorer = TopExp_Explorer(shape, TopAbs_SHELL)
    while explorer.More():
        yield TopoDS.Shell_s(explorer.Current())
        explorer.Next()
    loose = TopExp_Explorer(shape, TopAbs_FACE, TopAbs_SHELL)
    if loose.More():
        builder = BRep_Builder()
        shell = TopoDS_Shell()
        builder.MakeShell(shell)
        while loose.More():
            builder.Add(shell, loose.Current())
            loose.Next()
        yield shell


def _sewn_body(body: MeshBody, welded: _Welded, *, tolerance: float) -> _BuiltBody:
    faces = BRepBuilderAPI_MakeShapeOnMesh(_triangulation(welded)).Shape()
    sewing = BRepBuilderAPI_Sewing(tolerance)
    sewing.Add(faces)
    sewing.Perform()
    sewn = sewing.SewedShape()
    pieces: list[Any] = []
    solids = open_shells = 0
    for shell in _iter_shells(sewn):
        if BRep_Tool.IsClosed_s(shell):
            maker = BRepBuilderAPI_MakeSolid(shell)
            maker.Build()
            if maker.IsDone():
                solid = TopoDS.Solid_s(maker.Shape())
                BRepLib.OrientClosedSolid_s(solid)
                pieces.append(solid)
                solids += 1
                continue
        pieces.append(shell)
        open_shells += 1
    if not pieces:
        raise StepExportError(f"body {body.name!r}: sewing produced no shell")
    if len(pieces) == 1:
        shape = pieces[0]
    else:
        builder = BRep_Builder()
        compound = TopoDS_Compound()
        builder.MakeCompound(compound)
        for piece in pieces:
            builder.Add(compound, piece)
        shape = compound
    return _BuiltBody(
        body=body,
        shape=shape,
        solids=solids,
        open_shells=open_shells,
        free_edges=sewing.NbFreeEdges(),
    )


def _tessellated_body(body: MeshBody, welded: _Welded) -> _BuiltBody:
    builder = BRep_Builder()
    face = TopoDS_Face()
    builder.MakeFace(face, _triangulation(welded))
    shell = TopoDS_Shell()
    builder.MakeShell(shell)
    builder.Add(shell, face)
    return _BuiltBody(body=body, shape=shell, solids=0, open_shells=0, free_edges=0)


def _volume(shape: Any) -> float:
    total = 0.0
    explorer = TopExp_Explorer(shape, TopAbs_SOLID)
    properties = GProp_GProps()
    while explorer.More():
        BRepGProp.VolumeProperties_s(explorer.Current(), properties)
        total += properties.Mass()
        explorer.Next()
    return total


def _resolve_bodies(
    source: MeshScene | bytes | bytearray | memoryview | Iterable[MeshBody],
) -> tuple[list[MeshBody], int]:
    if isinstance(source, MeshScene):
        return bodies_from_scene(source)
    if isinstance(source, bytes | bytearray | memoryview):
        try:
            return bodies_from_scene(read_glb(bytes(source)))
        except GlbReadError as exc:
            raise StepExportError(f"cannot read the GLB: {exc}") from exc
    bodies = list(source)
    if not all(isinstance(body, MeshBody) for body in bodies):
        raise TypeError("source must be a MeshScene, GLB bytes or MeshBody items")
    return bodies, 0


def mesh_to_step(
    source: MeshScene | bytes | bytearray | memoryview | Iterable[MeshBody],
    dst: Path | str,
    *,
    schema: StepSchema = "AP214",
    mode: MeshStepMode = "solid",
    max_triangles: int | None = DEFAULT_MAX_TRIANGLES,
    sewing_tolerance: float = 1e-6,
    name: str | None = None,
    check: bool = True,
) -> MeshStepResult:
    """Write the triangles of ``source`` as STEP, one body per placed mesh.

    ``source`` is a :class:`cad3d_ir.mesh.MeshScene`, the GLB bytes, or
    :class:`MeshBody` items in millimetres. Identical vertices are welded and
    zero-area triangles dropped before anything is built. ``mode="solid"``
    sews the triangles into closed solids (open shells stay shells and are
    reported); ``mode="tessellated"`` writes AP242 triangulated faces and
    forces ``schema="AP242"``. ``max_triangles`` (``None`` for no cap) raises
    :class:`MeshTooLargeError` before geometry is built. ``name`` labels the
    single body or the assembly root; body names come from the GLB nodes.
    ``check`` validates the solids with ``BRepCheck_Analyzer`` and measures
    their volume.
    """
    _validate(schema, "mm")
    if mode not in ("solid", "tessellated"):
        raise ValueError(f"mode must be 'solid' or 'tessellated', not {mode!r}")
    target = Path(dst)
    bodies, merged_colors = _resolve_bodies(source)
    welded = [(body, _weld(body)) for body in bodies]
    welded = [(body, data) for body, data in welded if data.faces]
    triangles = sum(len(data.faces) for _, data in welded)
    degenerate = sum(data.degenerate for _, data in welded)
    if not triangles:
        raise StepExportError("the source holds no triangles to write")
    if max_triangles is not None and triangles > max_triangles:
        raise MeshTooLargeError(triangles=triangles, limit=max_triangles)

    diagnostics: list[ImportDiagnostic] = []
    written_schema: StepSchema = schema
    if mode == "tessellated":
        written_schema = "AP242"
        diagnostics.append(
            ImportDiagnostic(
                code="STEP_MESH_TESSELLATED",
                severity="info",
                action="approximated",
                message=(
                    "The mesh was written as AP242 triangulated faces: compact, "
                    "but only CAD systems that read tessellated STEP can open it."
                ),
                details={"requested_schema": schema, "triangles": triangles},
            )
        )
    else:
        diagnostics.append(
            ImportDiagnostic(
                code="STEP_MESH_SOLID_FACETED",
                severity="info",
                action="approximated",
                message=(
                    "The STEP was built from the display mesh: every face is a "
                    "planar triangle. It opens in any CAD but is not editable "
                    "B-rep geometry (no cylinders, fillets or dimensions)."
                ),
                details={"triangles": triangles, "bodies": len(welded)},
            )
        )

    try:
        built = [
            _tessellated_body(body, data)
            if mode == "tessellated"
            else _sewn_body(body, data, tolerance=sewing_tolerance)
            for body, data in welded
        ]
    except Standard_Failure as exc:
        raise StepExportError(f"OCCT failed while building the shape: {exc}") from exc

    document = _new_document()
    shape_tool = XCAFDoc_DocumentTool.ShapeTool_s(document.Main())
    color_tool = XCAFDoc_DocumentTool.ColorTool_s(document.Main())
    if len(built) == 1:
        (item,) = built
        label = shape_tool.AddShape(item.shape, False)
        _set_name(label, (name or item.body.name or _DEFAULT_BODY_NAME).strip())
        if item.body.color is not None:
            color_tool.SetColor(label, _rgba(item.body.color), XCAFDoc_ColorSurf)
    else:
        builder = BRep_Builder()
        compound = TopoDS_Compound()
        builder.MakeCompound(compound)
        for item in built:
            builder.Add(compound, item.shape)
        root = shape_tool.AddShape(compound, True)
        _set_name(root, (name or _DEFAULT_ASSEMBLY_NAME).strip())
        components = TDF_LabelSequence()
        shape_tool.GetComponents_s(root, components)
        for position in range(1, min(components.Length(), len(built)) + 1):
            item = built[position - 1]
            component = components.Value(position)
            referred = TDF_Label()
            body_name = item.body.name or f"{_DEFAULT_BODY_NAME}-{position}"
            if shape_tool.GetReferredShape_s(component, referred):
                _set_name(referred, body_name)
                if item.body.color is not None:
                    color_tool.SetColor(
                        referred, _rgba(item.body.color), XCAFDoc_ColorSurf
                    )
            _set_name(component, f"{body_name}:1")

    byte_length = _write_document(
        document,
        target,
        schema=written_schema,
        unit="mm",
        tessellated=mode == "tessellated",
    )

    solids = sum(item.solids for item in built)
    open_shells = sum(item.open_shells for item in built)
    free_edges = sum(item.free_edges for item in built)
    if open_shells:
        diagnostics.append(
            ImportDiagnostic(
                code="STEP_MESH_FREE_EDGES",
                severity="warning",
                action="unresolved",
                message=(
                    f"{open_shells} shell(s) did not close ({free_edges} free edges) "
                    "and were written as open shells instead of solids."
                ),
                details={"open_shells": open_shells, "free_edges": free_edges},
            )
        )
    if degenerate:
        diagnostics.append(
            ImportDiagnostic(
                code="STEP_MESH_DEGENERATE_DROPPED",
                severity="info",
                action="skipped",
                message=f"{degenerate} zero-area triangle(s) were left out.",
                details={"triangles": degenerate},
            )
        )
    if merged_colors:
        diagnostics.append(
            ImportDiagnostic(
                code="STEP_MESH_COLORS_MERGED",
                severity="info",
                action="approximated",
                message=(
                    f"{merged_colors} body(ies) had several colours; each body was "
                    "written with its first colour."
                ),
                details={"bodies": merged_colors},
            )
        )
    volume: float | None = None
    if check and mode == "solid":
        volume = 0.0
        invalid = 0
        for item in built:
            volume += _volume(item.shape)
            if not BRepCheck_Analyzer(item.shape).IsValid():
                invalid += 1
        if invalid:
            diagnostics.append(
                ImportDiagnostic(
                    code="STEP_MESH_SHAPE_INVALID",
                    severity="warning",
                    action="unresolved",
                    message=f"{invalid} body(ies) failed BRepCheck after sewing.",
                    details={"bodies": invalid},
                )
            )
    return MeshStepResult(
        path=target,
        byte_length=byte_length,
        schema=written_schema,
        mode=mode,
        bodies=len(built),
        triangles=triangles,
        vertices=sum(len(data.points) for _, data in welded),
        degenerate_triangles=degenerate,
        solids=solids,
        open_shells=open_shells,
        free_edges=free_edges,
        volume_mm3=volume,
        names=tuple(item.body.name for item in built),
        diagnostics=diagnostics,
    )


__all__ = [
    "DEFAULT_MAX_TRIANGLES",
    "STEP_SCHEMAS",
    "STEP_UNITS",
    "MeshBody",
    "MeshStepMode",
    "MeshStepResult",
    "SourceFormat",
    "StepExportResult",
    "StepSchema",
    "StepUnit",
    "bodies_from_scene",
    "export_step",
    "mesh_to_step",
    "quiet_occt",
    "source_format_of",
]
