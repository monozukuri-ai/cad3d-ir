"""Write the triangles of a mesh scene as STEP text directly, without OCCT shapes.

:func:`cad3d_ir_step.export.mesh_to_step` builds an OCCT face per triangle and
sews them with ``BRepBuilderAPI_Sewing``, whose time grows with the square of
the triangle count. Here the topology comes from the mesh itself:

* every mesh is written once, as a part, and each node that places it becomes
  an assembly component (``NEXT_ASSEMBLY_USAGE_OCCURRENCE`` with an
  ``ITEM_DEFINED_TRANSFORMATION``). A placement that is not a rotation and a
  translation (a mirror or a scale) gets its own part with the transform
  applied to the points;
* the triangles of all vertex sets of a mesh are welded on identical
  positions, so the faces a CAD writer tessellated separately join again;
* two triangles that use an edge in opposite directions share one
  ``EDGE_CURVE``. Triangles are reoriented across edges where that keeps the
  orientation consistent. A connected component whose every edge is shared
  this way is closed and becomes a ``MANIFOLD_SOLID_BREP``, oriented outwards;
  a part with any other component is written as a ``SHELL_BASED_SURFACE_MODEL``
  of open and closed shells;
* each triangle is an ``ADVANCED_FACE`` on a ``PLANE`` bounded by three
  ``LINE`` edges without p-curves.

The file therefore opens wherever AP214 / AP242 B-rep is read, but every face
is a planar triangle: it is not editable geometry. Colours come from the
primitive materials: each solid or surface model takes its most common
colour and triangles of another colour are styled one by one. Output is
written as it is produced, one part at a time, so memory stays bounded by the
largest part.

With ``representation="tessellated"`` (AP242 only) the same welded parts are
written as AP242 tessellated geometry instead of B-rep: one
``COORDINATES_LIST`` per part, a ``TRIANGULATED_FACE`` per vertex set of the
source mesh (one CAD face, for writers that tessellate per face) carrying the
mesh's own vertex normals, grouped into a ``TESSELLATED_SOLID`` where the
triangles close and a ``TESSELLATED_SHELL`` elsewhere. That form is about a
tenth of the size of the B-rep one and loads as a mesh (a graphics body) in
CAD that reads tessellated AP242; it has no faces to edit or measure.
"""

from __future__ import annotations

import math
from array import array
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Final, Literal, TextIO

from cad3d_ir import ImportDiagnostic
from cad3d_ir.mesh import MeshScene
from cad3d_ir.mesh.scene import (
    MODE_TRIANGLES,
    SceneMesh,
    SceneNode,
    transform_normals,
)

from cad3d_ir_step.errors import StepExportError

DirectSchema = Literal["AP214", "AP242"]
EdgeCurve = Literal["line", "polyline"]
Representation = Literal["brep", "tessellated"]

_SCHEMAS: Final = {
    "AP214": (
        "AUTOMOTIVE_DESIGN { 1 0 10303 214 1 1 1 1 }",
        "'automotive_design',2000",
        "core data for automotive mechanical design processes",
    ),
    "AP242": (
        "AP242_MANAGED_MODEL_BASED_3D_ENGINEERING_MIM_LF {1 0 10303 442 1 1 4 }",
        "'ap242_managed_model_based_3d_engineering',2013",
        "Managed model based 3d engineering",
    ),
}
_RIGID_TOLERANCE: Final = 1e-3
"""A placement whose rotation columns are unit length and orthogonal to this
tolerance is written as a rotation and a translation (eDrawings stores its
matrices in single precision, so they are off by about 1e-5)."""
_FLUSH_LINES: Final = 65536
_LIST_CHUNK: Final = 8
"""Items per line in the long aggregates of tessellated entities."""
_DEFAULT_PART_NAME: Final = "body"
_DEFAULT_ASSEMBLY_NAME: Final = "assembly"

Matrix = tuple[tuple[float, float, float, float], ...]
Rgb = tuple[float, float, float]


@dataclass(frozen=True, slots=True)
class DirectStepResult:
    """What :func:`write_mesh_step` wrote."""

    path: Path
    byte_length: int
    schema: DirectSchema
    parts: int
    placements: int
    baked_placements: int
    triangles_placed: int
    triangles_written: int
    vertices_written: int
    degenerate_triangles: int
    solids: int
    shells: int
    open_shells: int
    boundary_edges: int
    split_edges: int
    styled_faces: int
    names: tuple[str, ...]
    representation: Representation = "brep"
    tessellated_faces: int = 0
    """``TRIANGULATED_FACE`` entities written (tessellated representation)."""
    diagnostics: list[ImportDiagnostic] = field(default_factory=list)


# -- text ------------------------------------------------------------------------


def _real(value: float, digits: int = 10) -> str:
    """A Part 21 REAL: always a decimal point, upper-case exponent."""
    text = f"{value:.{digits}G}"
    if "E" in text:
        mantissa, exponent = text.split("E")
        if "." not in mantissa:
            mantissa += "."
        return f"{mantissa}E{exponent}"
    if "." not in text:
        text += "."
    return text


def _text(value: str) -> str:
    """A Part 21 string literal: quotes doubled, non-ASCII as ``\\X2\\`` runs."""
    out: list[str] = []
    wide: list[str] = []
    for char in value:
        code = ord(char)
        if 0x20 <= code < 0x7F:
            if wide:
                out.append("\\X2\\" + "".join(wide) + "\\X0\\")
                wide.clear()
            out.append("''" if char == "'" else "\\\\" if char == "\\" else char)
        else:
            encoded = char.encode("utf-16-be")
            wide.extend(
                f"{encoded[i]:02X}{encoded[i + 1]:02X}"
                for i in range(0, len(encoded), 2)
            )
    if wide:
        out.append("\\X2\\" + "".join(wide) + "\\X0\\")
    return "'" + "".join(out) + "'"


def _aggregate(items: list[str]) -> str:
    """A Part 21 aggregate, wrapped every :data:`_LIST_CHUNK` items."""
    if not items:
        return "()"
    lines = [
        ",".join(items[start : start + _LIST_CHUNK])
        for start in range(0, len(items), _LIST_CHUNK)
    ]
    return "(" + ",\n".join(lines) + ")"


def _srgb(linear: float) -> float:
    """glTF base colours are linear; STEP colours are display (sRGB) values."""
    linear = min(max(linear, 0.0), 1.0)
    if linear <= 0.0031308:
        return 12.92 * linear
    return 1.055 * linear ** (1.0 / 2.4) - 0.055


class _Writer:
    """Numbers entities and writes them in batches."""

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream
        self._lines: list[str] = []
        self.next_id = 1

    def add(self, body: str) -> int:
        entity = self.next_id
        self.next_id += 1
        self._lines.append(f"#{entity}={body};\n")
        if len(self._lines) >= _FLUSH_LINES:
            self.flush()
        return entity

    def reserve(self) -> int:
        """Number an entity now and write it later with :meth:`put`."""
        entity = self.next_id
        self.next_id += 1
        return entity

    def put(self, entity: int, body: str) -> None:
        self._lines.append(f"#{entity}={body};\n")
        if len(self._lines) >= _FLUSH_LINES:
            self.flush()

    def flush(self) -> None:
        if self._lines:
            self._stream.write("".join(self._lines))
            self._lines.clear()


# -- geometry ----------------------------------------------------------------------


@dataclass(slots=True)
class _Part:
    """One welded mesh: points, oriented triangles and how they connect."""

    name: str
    points: list[tuple[float, float, float]]
    faces: list[tuple[int, int, int]]
    colors: list[int]
    degenerate: int
    groups: list[int] = field(default_factory=list)
    """Per face: the vertex set it came from (one CAD face in most writers)."""
    remaps: dict[int, list[int]] = field(default_factory=dict)
    """Per vertex set: the welded point of each of its vertices (kept only
    when normals are wanted)."""
    normals: dict[int, array | None] = field(default_factory=dict)
    """Per vertex set: its unit normals, transformed when the placement is
    baked (kept only when normals are wanted)."""


@dataclass(slots=True)
class _Topology:
    """Shared edges, components and which components close."""

    # per face slot (face * 3 + k): the edge entity index and whether the face
    # runs along the edge (True) or against it
    slot_edge: list[int]
    slot_forward: list[bool]
    edges: list[tuple[int, int]]
    components: list[list[int]]
    closed: list[bool]
    boundary_edges: int
    split_edges: int


def _is_rigid(matrix: Matrix) -> bool:
    columns = [[matrix[row][col] for row in range(3)] for col in range(3)]
    for i in range(3):
        for j in range(i, 3):
            dot = sum(a * b for a, b in zip(columns[i], columns[j], strict=True))
            if abs(dot - (1.0 if i == j else 0.0)) > _RIGID_TOLERANCE:
                return False
    x, y, z = columns
    determinant = (
        x[0] * (y[1] * z[2] - y[2] * z[1])
        - y[0] * (x[1] * z[2] - x[2] * z[1])
        + z[0] * (x[1] * y[2] - x[2] * y[1])
    )
    return determinant > 0.0


def _material_rgb(scene: MeshScene, material: int | None) -> Rgb | None:
    if material is None or not 0 <= material < len(scene.materials):
        return None
    pbr = scene.materials[material].get("pbrMetallicRoughness")
    factor = pbr.get("baseColorFactor") if isinstance(pbr, dict) else None
    if isinstance(factor, list) and len(factor) == 4:
        return (float(factor[0]), float(factor[1]), float(factor[2]))
    return None


def _build_part(
    scene: MeshScene,
    mesh: SceneMesh,
    name: str,
    *,
    factor: float,
    matrix: Matrix | None,
    palette: dict[Rgb, int],
    keep_normals: bool = False,
) -> _Part:
    """Weld every triangle primitive of ``mesh`` into one point list (mm)."""
    lookup: dict[tuple[float, float, float], int] = {}
    points: list[tuple[float, float, float]] = []
    faces: list[tuple[int, int, int]] = []
    colors: list[int] = []
    groups: list[int] = []
    normals: dict[int, array | None] = {}
    degenerate = 0
    remaps: dict[int, list[int]] = {}
    if matrix is not None:
        (m00, m01, m02, m03), (m10, m11, m12, m13), (m20, m21, m22, m23) = (
            matrix[0],
            matrix[1],
            matrix[2],
        )
    for primitive in mesh.primitives:
        if primitive.mode != MODE_TRIANGLES or not primitive.indices:
            continue
        remap = remaps.get(primitive.vertex_set)
        if remap is None:
            remap = []
            values = iter(primitive.positions)
            for x, y, z in zip(values, values, values, strict=True):
                x, y, z = x * factor, y * factor, z * factor
                if matrix is not None:
                    x, y, z = (
                        m00 * x + m01 * y + m02 * z + m03 * factor,
                        m10 * x + m11 * y + m12 * z + m13 * factor,
                        m20 * x + m21 * y + m22 * z + m23 * factor,
                    )
                key = (x, y, z)
                index = lookup.get(key)
                if index is None:
                    index = len(points)
                    lookup[key] = index
                    points.append(key)
                remap.append(index)
            remaps[primitive.vertex_set] = remap
            if keep_normals:
                source = primitive.normals
                if source is not None and matrix is not None:
                    source = transform_normals(source, matrix)
                normals[primitive.vertex_set] = source
        rgb = _material_rgb(scene, primitive.material)
        color = -1 if rgb is None else palette.setdefault(rgb, len(palette))
        indices = primitive.indices
        count = len(remap)
        for start in range(0, len(indices) - 2, 3):
            ia, ib, ic = indices[start], indices[start + 1], indices[start + 2]
            if not (ia < count and ib < count and ic < count):
                raise StepExportError(
                    f"mesh {name!r}: triangle indices exceed the vertices"
                )
            a, b, c = remap[ia], remap[ib], remap[ic]
            if a in (b, c) or b == c:
                degenerate += 1
                continue
            (ax, ay, az), (bx, by, bz), (cx, cy, cz) = points[a], points[b], points[c]
            ux, uy, uz = bx - ax, by - ay, bz - az
            vx, vy, vz = cx - ax, cy - ay, cz - az
            if (
                uy * vz - uz * vy == 0.0
                and uz * vx - ux * vz == 0.0
                and ux * vy - uy * vx == 0.0
            ):
                degenerate += 1
                continue
            faces.append((a, b, c))
            colors.append(color)
            groups.append(primitive.vertex_set)
    return _Part(
        name=name,
        points=points,
        faces=faces,
        colors=colors,
        degenerate=degenerate,
        groups=groups,
        remaps=remaps if keep_normals else {},
        normals=normals,
    )


def _topology(part: _Part) -> _Topology:
    """Share edges, orient faces consistently and find the closed components.

    Faces in ``part.faces`` are reordered in place where a component needs
    flipping; ``slot_edge`` / ``slot_forward`` describe the result.
    """
    faces = part.faces
    count = len(part.points)
    first: dict[int, int] = {}
    second: dict[int, int] = {}
    crowded: dict[int, list[int]] = {}
    for face, (a, b, c) in enumerate(faces):
        base = face * 3
        for slot, (u, v) in enumerate(((a, b), (b, c), (c, a))):
            key = u * count + v if u < v else v * count + u
            use = base + slot
            if key not in first:
                first[key] = use
            elif key not in second:
                second[key] = use
            else:
                crowded.setdefault(key, []).append(use)

    def ends(use: int) -> tuple[int, int]:
        a, b, c = faces[use // 3]
        return ((a, b), (b, c), (c, a))[use % 3]

    # neighbour of a slot across a two-use edge (-1 for boundary / non-manifold)
    partner = [-1] * (len(faces) * 3)
    for key, use in second.items():
        if key in crowded:
            continue
        other = first[key]
        partner[use] = other
        partner[other] = use

    # orient: BFS over manifold edges; flip[f] is relative to the seed
    flip = [-1] * len(faces)
    components: list[list[int]] = []
    orientable: list[bool] = []
    for seed in range(len(faces)):
        if flip[seed] != -1:
            continue
        flip[seed] = 0
        members = [seed]
        stack = [seed]
        consistent = True
        while stack:
            face = stack.pop()
            for slot in range(3):
                use = face * 3 + slot
                other = partner[use]
                if other < 0:
                    continue
                neighbour = other // 3
                same_direction = ends(use) == ends(other)
                wanted = flip[face] ^ (1 if same_direction else 0)
                if flip[neighbour] == -1:
                    flip[neighbour] = wanted
                    members.append(neighbour)
                    stack.append(neighbour)
                elif flip[neighbour] != wanted:
                    consistent = False
        components.append(members)
        orientable.append(consistent)
    for face, flipped in enumerate(flip):
        if flipped:
            a, b, c = faces[face]
            faces[face] = (a, c, b)
    # flipping changes which slot holds which edge: rebuild the slot maps
    first.clear()
    second.clear()
    crowded.clear()
    for face, (a, b, c) in enumerate(faces):
        base = face * 3
        for slot, (u, v) in enumerate(((a, b), (b, c), (c, a))):
            key = u * count + v if u < v else v * count + u
            use = base + slot
            if key not in first:
                first[key] = use
            elif key not in second:
                second[key] = use
            else:
                crowded.setdefault(key, []).append(use)

    slot_edge = [-1] * (len(faces) * 3)
    slot_forward = [True] * (len(faces) * 3)
    edges: list[tuple[int, int]] = []
    open_faces: set[int] = set()
    boundary = split = 0
    for key, use in first.items():
        other = second.get(key)
        shared = (
            other is not None
            and key not in crowded
            and ends(use) == tuple(reversed(ends(other)))
        )
        if shared:
            slot_edge[use] = slot_edge[other] = len(edges)
            slot_forward[other] = False
            edges.append(ends(use))
            continue
        uses = [use] + ([other] if other is not None else []) + crowded.get(key, [])
        if len(uses) == 1:
            boundary += 1
        else:
            split += 1
        for each in uses:
            slot_edge[each] = len(edges)
            edges.append(ends(each))
            open_faces.add(each // 3)

    closed = []
    for members, consistent in zip(components, orientable, strict=True):
        is_closed = consistent and not any(face in open_faces for face in members)
        if is_closed and _signed_volume(part, members) < 0.0:
            for face in members:
                a, b, c = faces[face]
                faces[face] = (a, c, b)
                for slot in range(3):
                    slot_forward[face * 3 + slot] = not slot_forward[face * 3 + slot]
            _reverse_slots(faces, members, slot_edge, slot_forward)
        closed.append(is_closed)
    return _Topology(
        slot_edge=slot_edge,
        slot_forward=slot_forward,
        edges=edges,
        components=components,
        closed=closed,
        boundary_edges=boundary,
        split_edges=split,
    )


def _reverse_slots(
    faces: list[tuple[int, int, int]],
    members: list[int],
    slot_edge: list[int],
    slot_forward: list[bool],
) -> None:
    """Move the slot data of faces flipped from ``(a, b, c)`` to ``(a, c, b)``.

    The old slots were (a,b), (b,c), (c,a) and the new ones are (a,c), (c,b),
    (b,a): old slot 2 becomes slot 0, slot 1 stays and slot 0 becomes slot 2
    (their directions were already reversed by the caller).
    """
    for face in members:
        base = face * 3
        e0, e1, e2 = slot_edge[base], slot_edge[base + 1], slot_edge[base + 2]
        f0, f1, f2 = slot_forward[base], slot_forward[base + 1], slot_forward[base + 2]
        slot_edge[base], slot_edge[base + 1], slot_edge[base + 2] = e2, e1, e0
        slot_forward[base], slot_forward[base + 1], slot_forward[base + 2] = f2, f1, f0


def _signed_volume(part: _Part, members: list[int]) -> float:
    points = part.points
    total = 0.0
    for face in members:
        a, b, c = part.faces[face]
        (ax, ay, az), (bx, by, bz), (cx, cy, cz) = points[a], points[b], points[c]
        total += (
            ax * (by * cz - bz * cy)
            - ay * (bx * cz - bz * cx)
            + az * (bx * cy - by * cx)
        )
    return total / 6.0


# -- writing -----------------------------------------------------------------------


@dataclass(slots=True)
class _Context:
    application: int
    product_context: int
    definition_context: int
    representation_context: int
    origin: int
    styles: dict[int, int]


@dataclass(slots=True)
class _WrittenPart:
    representation: int
    definition: int
    triangles: int
    vertices: int
    solids: int
    shells: int
    open_shells: int
    boundary_edges: int
    split_edges: int
    styled_faces: int
    degenerate: int
    tessellated_faces: int = 0


def _header(
    stream: TextIO, schema: DirectSchema, name: str, representation: Representation
) -> None:
    file_schema = _SCHEMAS[schema][0]
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    description = (
        "mesh written as AP242 tessellated geometry"
        if representation == "tessellated"
        else "mesh written as planar triangles"
    )
    stream.write(
        "ISO-10303-21;\nHEADER;\n"
        f"FILE_DESCRIPTION(('{description}'),'2;1');\n"
        f"FILE_NAME({_text(name)},'{stamp}',(''),(''),'cad3d-ir-step','cad3d-ir-step','');\n"
        f"FILE_SCHEMA(('{file_schema}'));\nENDSEC;\nDATA;\n"
    )


def _context(writer: _Writer, schema: DirectSchema) -> _Context:
    _, protocol, application_name = _SCHEMAS[schema]
    application = writer.add(f"APPLICATION_CONTEXT('{application_name}')")
    writer.add(
        "APPLICATION_PROTOCOL_DEFINITION('international standard',"
        f"{protocol},#{application})"
    )
    product_context = writer.add(f"PRODUCT_CONTEXT('',#{application},'mechanical')")
    definition_context = writer.add(
        f"PRODUCT_DEFINITION_CONTEXT('part definition',#{application},'design')"
    )
    length = writer.add("( LENGTH_UNIT() NAMED_UNIT(*) SI_UNIT(.MILLI.,.METRE.) )")
    angle = writer.add("( NAMED_UNIT(*) PLANE_ANGLE_UNIT() SI_UNIT($,.RADIAN.) )")
    solid_angle = writer.add(
        "( NAMED_UNIT(*) SI_UNIT($,.STERADIAN.) SOLID_ANGLE_UNIT() )"
    )
    uncertainty = writer.add(
        f"UNCERTAINTY_MEASURE_WITH_UNIT(LENGTH_MEASURE(1.E-07),#{length},"
        "'distance_accuracy_value','confusion accuracy')"
    )
    representation_context = writer.add(
        "( GEOMETRIC_REPRESENTATION_CONTEXT(3)"
        f" GLOBAL_UNCERTAINTY_ASSIGNED_CONTEXT((#{uncertainty}))"
        f" GLOBAL_UNIT_ASSIGNED_CONTEXT((#{length},#{angle},#{solid_angle}))"
        " REPRESENTATION_CONTEXT('Context #1','3D Context with UNIT and UNCERTAINTY') )"
    )
    origin = _axis(writer, (0.0, 0.0, 0.0), (0.0, 0.0, 1.0), (1.0, 0.0, 0.0))
    return _Context(
        application=application,
        product_context=product_context,
        definition_context=definition_context,
        representation_context=representation_context,
        origin=origin,
        styles={},
    )


def _point(writer: _Writer, point: tuple[float, float, float]) -> int:
    x, y, z = point
    return writer.add(f"CARTESIAN_POINT('',({_real(x)},{_real(y)},{_real(z)}))")


def _direction(writer: _Writer, vector: tuple[float, float, float]) -> int:
    x, y, z = vector
    return writer.add(f"DIRECTION('',({_real(x)},{_real(y)},{_real(z)}))")


def _axis(
    writer: _Writer,
    location: tuple[float, float, float],
    axis: tuple[float, float, float],
    reference: tuple[float, float, float],
) -> int:
    point = _point(writer, location)
    z = _direction(writer, axis)
    x = _direction(writer, reference)
    return writer.add(f"AXIS2_PLACEMENT_3D('',#{point},#{z},#{x})")


def _style(
    writer: _Writer, context: _Context, palette_rgb: list[Rgb], color: int
) -> int:
    """The shared PRESENTATION_STYLE_ASSIGNMENT of one palette colour."""
    style = context.styles.get(color)
    if style is not None:
        return style
    r, g, b = (_srgb(value) for value in palette_rgb[color])
    rgb = writer.add(f"COLOUR_RGB('',{_real(r)},{_real(g)},{_real(b)})")
    fill_colour = writer.add(f"FILL_AREA_STYLE_COLOUR('',#{rgb})")
    fill = writer.add(f"FILL_AREA_STYLE('',(#{fill_colour}))")
    area = writer.add(f"SURFACE_STYLE_FILL_AREA(#{fill})")
    side = writer.add(f"SURFACE_SIDE_STYLE('',(#{area}))")
    usage = writer.add(f"SURFACE_STYLE_USAGE(.BOTH.,#{side})")
    style = writer.add(f"PRESENTATION_STYLE_ASSIGNMENT((#{usage}))")
    context.styles[color] = style
    return style


def _refs(ids: list[int]) -> str:
    return ",".join(f"#{entity}" for entity in ids)


def _product(writer: _Writer, context: _Context, name: str, representation: int) -> int:
    """PRODUCT … SHAPE_DEFINITION_REPRESENTATION; returns the PRODUCT_DEFINITION."""
    label = _text(name)
    product = writer.add(f"PRODUCT({label},{label},'',(#{context.product_context}))")
    writer.add(f"PRODUCT_RELATED_PRODUCT_CATEGORY('part',$,(#{product}))")
    formation = writer.add(f"PRODUCT_DEFINITION_FORMATION('','',#{product})")
    definition = writer.add(
        f"PRODUCT_DEFINITION('design','',#{formation},#{context.definition_context})"
    )
    shape = writer.add(f"PRODUCT_DEFINITION_SHAPE('','',#{definition})")
    writer.add(f"SHAPE_DEFINITION_REPRESENTATION(#{shape},#{representation})")
    return definition


def _write_part(
    writer: _Writer,
    context: _Context,
    part: _Part,
    palette_rgb: list[Rgb],
    *,
    edge_curve: EdgeCurve,
) -> _WrittenPart:
    topology = _topology(part)
    faces = part.faces
    used = sorted({vertex for face in faces for vertex in face})
    point_ids: dict[int, int] = {}
    vertex_ids: dict[int, int] = {}
    for vertex in used:
        point_ids[vertex] = _point(writer, part.points[vertex])
    for vertex in used:
        vertex_ids[vertex] = writer.add(f"VERTEX_POINT('',#{point_ids[vertex]})")
    points = part.points
    # identical directions, line vectors and planes are written once per part
    directions: dict[str, int] = {}
    vectors: dict[int, int] = {}
    planes: dict[tuple[str, str], int] = {}

    def direction(x: float, y: float, z: float) -> int:
        text = f"{_real(x)},{_real(y)},{_real(z)}"
        entity = directions.get(text)
        if entity is None:
            entity = writer.add(f"DIRECTION('',({text}))")
            directions[text] = entity
        return entity

    edge_ids: list[int] = []
    for u, v in topology.edges:
        if edge_curve == "polyline":
            curve = writer.add(f"POLYLINE('',(#{point_ids[u]},#{point_ids[v]}))")
        else:
            (ux, uy, uz), (vx, vy, vz) = points[u], points[v]
            dx, dy, dz = vx - ux, vy - uy, vz - uz
            length = math.sqrt(dx * dx + dy * dy + dz * dz)
            along = direction(dx / length, dy / length, dz / length)
            vector = vectors.get(along)
            if vector is None:
                vector = writer.add(f"VECTOR('',#{along},1.)")
                vectors[along] = vector
            curve = writer.add(f"LINE('',#{point_ids[u]},#{vector})")
        edge_ids.append(
            writer.add(f"EDGE_CURVE('',#{vertex_ids[u]},#{vertex_ids[v]},#{curve},.T.)")
        )
    face_ids: list[int] = []
    slot_edge, slot_forward = topology.slot_edge, topology.slot_forward
    for face, (a, b, c) in enumerate(faces):
        (ax, ay, az), (bx, by, bz), (cx, cy, cz) = points[a], points[b], points[c]
        ux, uy, uz = bx - ax, by - ay, bz - az
        vx, vy, vz = cx - ax, cy - ay, cz - az
        nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        norm = math.sqrt(nx * nx + ny * ny + nz * nz)
        nx, ny, nz = nx / norm, ny / norm, nz / norm
        normal_text = f"{_real(nx)},{_real(ny)},{_real(nz)}"
        plane_key = (normal_text, _real(nx * ax + ny * ay + nz * az))
        plane = planes.get(plane_key)
        if plane is None:
            normal = directions.get(normal_text)
            if normal is None:
                normal = writer.add(f"DIRECTION('',({normal_text}))")
                directions[normal_text] = normal
            placement = writer.add(
                f"AXIS2_PLACEMENT_3D('',#{point_ids[a]},#{normal},$)"
            )
            plane = writer.add(f"PLANE('',#{placement})")
            planes[plane_key] = plane
        base = face * 3
        oriented = [
            writer.add(
                f"ORIENTED_EDGE('',*,*,#{edge_ids[slot_edge[base + k]]},"
                f"{'.T.' if slot_forward[base + k] else '.F.'})"
            )
            for k in range(3)
        ]
        loop = writer.add(f"EDGE_LOOP('',({_refs(oriented)}))")
        bound = writer.add(f"FACE_OUTER_BOUND('',#{loop},.T.)")
        face_ids.append(writer.add(f"ADVANCED_FACE('',(#{bound}),#{plane},.T.)"))

    all_closed = all(topology.closed)
    items: list[int] = []
    item_faces: list[list[int]] = []
    shells = open_shells = solids = 0
    shell_ids: list[int] = []
    for members, closed in zip(topology.components, topology.closed, strict=True):
        kind = "CLOSED_SHELL" if closed else "OPEN_SHELL"
        shell = writer.add(f"{kind}('',({_refs([face_ids[f] for f in members])}))")
        shells += 1
        if not closed:
            open_shells += 1
        if all_closed:
            items.append(writer.add(f"MANIFOLD_SOLID_BREP('',#{shell})"))
            item_faces.append(members)
            solids += 1
        else:
            shell_ids.append(shell)
    if not all_closed:
        items.append(writer.add(f"SHELL_BASED_SURFACE_MODEL('',({_refs(shell_ids)}))"))
        item_faces.append([face for members in topology.components for face in members])
    kind = (
        "ADVANCED_BREP_SHAPE_REPRESENTATION"
        if all_closed
        else "MANIFOLD_SURFACE_SHAPE_REPRESENTATION"
    )
    representation = writer.add(
        f"{kind}('',(#{context.origin},{_refs(items)}),#{context.representation_context})"
    )

    styled: list[int] = []
    styled_faces = 0
    for item, members in zip(items, item_faces, strict=True):
        counts = Counter(
            part.colors[face] for face in members if part.colors[face] >= 0
        )
        if not counts:
            continue
        main = counts.most_common(1)[0][0]
        main_style = _style(writer, context, palette_rgb, main)
        styled.append(writer.add(f"STYLED_ITEM('color',(#{main_style}),#{item})"))
        for face in members:
            color = part.colors[face]
            if color >= 0 and color != main:
                style = _style(writer, context, palette_rgb, color)
                styled.append(
                    writer.add(f"STYLED_ITEM('color',(#{style}),#{face_ids[face]})")
                )
                styled_faces += 1
    if styled:
        writer.add(
            "MECHANICAL_DESIGN_GEOMETRIC_PRESENTATION_REPRESENTATION('',"
            f"({_refs(styled)}),#{context.representation_context})"
        )
    definition = _product(writer, context, part.name, representation)
    return _WrittenPart(
        representation=representation,
        definition=definition,
        triangles=len(faces),
        vertices=len(used),
        solids=solids,
        shells=shells,
        open_shells=open_shells,
        boundary_edges=topology.boundary_edges,
        split_edges=topology.split_edges,
        styled_faces=styled_faces,
        degenerate=part.degenerate,
    )


def _write_part_tessellated(
    writer: _Writer,
    context: _Context,
    part: _Part,
    palette_rgb: list[Rgb],
) -> _WrittenPart:
    """Write ``part`` as AP242 tessellated geometry.

    One ``COORDINATES_LIST`` holds the welded points. Each vertex set of the
    source mesh becomes a ``TRIANGULATED_FACE`` (its ``pnindex`` selects the
    points it uses, its normals are the mesh's own) and the vertex sets that
    share welded triangles are grouped into a ``TESSELLATED_SOLID`` when every
    connected component among them closes, else a ``TESSELLATED_SHELL``.
    """
    topology = _topology(part)
    faces = part.faces
    points = part.points
    used = sorted({vertex for face in faces for vertex in face})
    position = {vertex: index + 1 for index, vertex in enumerate(used)}
    coordinates = writer.add(
        f"COORDINATES_LIST('',{len(used)},"
        + _aggregate(
            [
                f"({_real(x, 9)},{_real(y, 9)},{_real(z, 9)})"
                for x, y, z in (points[vertex] for vertex in used)
            ]
        )
        + ")"
    )

    by_group: dict[int, list[int]] = {}
    for face, group in enumerate(part.groups):
        by_group.setdefault(group, []).append(face)
    # vertex sets joined by a connected component share one solid or shell
    parent = {group: group for group in by_group}

    def find(group: int) -> int:
        while parent[group] != group:
            parent[group] = parent[parent[group]]
            group = parent[group]
        return group

    for members in topology.components:
        root = find(part.groups[members[0]])
        for face in members:
            other = find(part.groups[face])
            if other != root:
                parent[other] = root
    closed_root = {find(group): True for group in by_group}
    for members, closed in zip(topology.components, topology.closed, strict=True):
        if not closed:
            closed_root[find(part.groups[members[0]])] = False

    face_entities: dict[int, int] = {}
    group_color: dict[int, int] = {}
    for group, members in by_group.items():
        source = part.normals.get(group)
        remap = part.remaps.get(group)
        inverse: dict[int, int] | None = None
        if source is not None and remap is not None:
            inverse = {}
            for original, welded in enumerate(remap):
                inverse.setdefault(welded, original)
        local: dict[int, int] = {}
        pnindex: list[str] = []
        normal_text: list[str] = []
        triangles: list[str] = []
        for face in members:
            corners = []
            for vertex in faces[face]:
                index = local.get(vertex)
                if index is None:
                    index = len(local) + 1
                    local[vertex] = index
                    pnindex.append(str(position[vertex]))
                    if inverse is not None and source is not None:
                        at = inverse[vertex] * 3
                        normal_text.append(
                            f"({_real(source[at], 6)},{_real(source[at + 1], 6)},"
                            f"{_real(source[at + 2], 6)})"
                        )
                corners.append(str(index))
            triangles.append(f"({','.join(corners)})")
        face_entities[group] = writer.add(
            f"TRIANGULATED_FACE('',#{coordinates},{len(local)},"
            f"{_aggregate(normal_text)},$,{_aggregate(pnindex)},{_aggregate(triangles)})"
        )
        counts = Counter(
            part.colors[face] for face in members if part.colors[face] >= 0
        )
        group_color[group] = counts.most_common(1)[0][0] if counts else -1

    containers: dict[int, list[int]] = {}
    for group in by_group:
        containers.setdefault(find(group), []).append(group)
    items: list[int] = []
    item_groups: list[list[int]] = []
    solids = 0
    for root, groups in containers.items():
        kind = "TESSELLATED_SOLID" if closed_root[root] else "TESSELLATED_SHELL"
        items.append(
            writer.add(f"{kind}('',({_refs([face_entities[g] for g in groups])}),$)")
        )
        item_groups.append(groups)
        if closed_root[root]:
            solids += 1
    representation = writer.add(
        f"TESSELLATED_SHAPE_REPRESENTATION('',(#{context.origin},{_refs(items)}),"
        f"#{context.representation_context})"
    )

    styled: list[int] = []
    styled_faces = 0
    for item, groups in zip(items, item_groups, strict=True):
        counts = Counter(
            part.colors[face]
            for group in groups
            for face in by_group[group]
            if part.colors[face] >= 0
        )
        if not counts:
            continue
        main = counts.most_common(1)[0][0]
        main_style = _style(writer, context, palette_rgb, main)
        styled.append(writer.add(f"STYLED_ITEM('color',(#{main_style}),#{item})"))
        for group in groups:
            color = group_color[group]
            if color >= 0 and color != main:
                style = _style(writer, context, palette_rgb, color)
                styled.append(
                    writer.add(
                        f"STYLED_ITEM('color',(#{style}),#{face_entities[group]})"
                    )
                )
                styled_faces += 1
    if styled:
        writer.add(
            "MECHANICAL_DESIGN_GEOMETRIC_PRESENTATION_REPRESENTATION('',"
            f"({_refs(styled)}),#{context.representation_context})"
        )
    definition = _product(writer, context, part.name, representation)
    return _WrittenPart(
        representation=representation,
        definition=definition,
        triangles=len(faces),
        vertices=len(used),
        solids=solids,
        shells=len(topology.components),
        open_shells=sum(1 for closed in topology.closed if not closed),
        boundary_edges=topology.boundary_edges,
        split_edges=topology.split_edges,
        styled_faces=styled_faces,
        degenerate=part.degenerate,
        tessellated_faces=len(face_entities),
    )


def _unit_vector(x: float, y: float, z: float) -> tuple[float, float, float]:
    norm = math.sqrt(x * x + y * y + z * z)
    return (x / norm, y / norm, z / norm)


def _placement_axis(writer: _Writer, matrix: Matrix, factor: float) -> int:
    """AXIS2_PLACEMENT_3D of a rigid node matrix (columns re-orthogonalised)."""
    z = _unit_vector(matrix[0][2], matrix[1][2], matrix[2][2])
    x0 = (matrix[0][0], matrix[1][0], matrix[2][0])
    dot = x0[0] * z[0] + x0[1] * z[1] + x0[2] * z[2]
    x = _unit_vector(x0[0] - dot * z[0], x0[1] - dot * z[1], x0[2] - dot * z[2])
    location = (matrix[0][3] * factor, matrix[1][3] * factor, matrix[2][3] * factor)
    return _axis(writer, location, z, x)


def _placements(scene: MeshScene) -> Iterator[tuple[SceneNode, SceneMesh]]:
    for node, mesh in scene.placements():
        if any(p.mode == MODE_TRIANGLES and p.indices for p in mesh.primitives):
            yield node, mesh


def _part_name(mesh: SceneMesh, node: SceneNode, used: dict[str, int]) -> str:
    base = (mesh.name or "").strip() or (node.name or "").strip() or _DEFAULT_PART_NAME
    seen = used.get(base, 0)
    used[base] = seen + 1
    return base if seen == 0 else f"{base}-{seen + 1}"


def write_mesh_step(
    scene: MeshScene,
    dst: Path | str,
    *,
    schema: DirectSchema = "AP214",
    name: str | None = None,
    edge_curve: EdgeCurve = "line",
    representation: Representation = "brep",
    normals: bool = True,
) -> DirectStepResult:
    """Write ``scene`` as STEP: one part per mesh, placed by its nodes.

    See the module docstring for the representations. ``name`` labels the
    assembly root, or the part when the scene places a single mesh once.
    ``edge_curve`` is the geometry of each B-rep edge: ``"line"`` (a ``LINE``
    through the start vertex) or ``"polyline"`` (a two-point ``POLYLINE``:
    smaller, also allowed in advanced B-rep, but less common).
    ``representation="tessellated"`` writes AP242 tessellated geometry instead
    of B-rep (``schema`` must be ``"AP242"``); ``normals`` then copies the
    mesh's vertex normals, which cost about a third of the size.
    """
    if schema not in _SCHEMAS:
        raise ValueError(f"schema must be one of {tuple(_SCHEMAS)}, not {schema!r}")
    if edge_curve not in ("line", "polyline"):
        raise ValueError(f"edge_curve must be 'line' or 'polyline', not {edge_curve!r}")
    if representation not in ("brep", "tessellated"):
        raise ValueError(
            f"representation must be 'brep' or 'tessellated', not {representation!r}"
        )
    if representation == "tessellated" and schema != "AP242":
        raise ValueError("tessellated geometry is AP242 only: pass schema='AP242'")
    target = Path(dst)
    factor = scene.length_unit_to_m * 1000.0
    placed = list(_placements(scene))
    if not placed:
        raise StepExportError("the scene places no triangles")
    palette: dict[Rgb, int] = {}
    tessellated = representation == "tessellated"
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="ascii", newline="\n") as stream:
        _header(stream, schema, name or target.stem, representation)
        writer = _Writer(stream)
        context = _context(writer, schema)
        written: dict[int, _WrittenPart] = {}
        names: list[str] = []
        used_names: dict[str, int] = {}
        baked = 0
        totals = Counter()

        def write(part: _Part) -> _WrittenPart:
            palette_rgb = [
                rgb for rgb, _ in sorted(palette.items(), key=lambda kv: kv[1])
            ]
            if tessellated:
                done = _write_part_tessellated(writer, context, part, palette_rgb)
            else:
                done = _write_part(
                    writer, context, part, palette_rgb, edge_curve=edge_curve
                )
            names.append(part.name)
            for key in (
                "triangles",
                "vertices",
                "solids",
                "shells",
                "open_shells",
                "boundary_edges",
                "split_edges",
                "styled_faces",
                "degenerate",
                "tessellated_faces",
            ):
                totals[key] += getattr(done, key)
            return done

        def build(mesh: SceneMesh, matrix: Matrix | None, label: str) -> _Part:
            return _build_part(
                scene,
                mesh,
                label,
                factor=factor,
                matrix=matrix,
                palette=palette,
                keep_normals=tessellated and normals,
            )

        triangles_placed = 0
        if len(placed) == 1:
            node, mesh = placed[0]
            label = (name or "").strip() or _part_name(mesh, node, used_names)
            part = build(mesh, node.matrix, label)
            if not part.faces:
                raise StepExportError("the scene holds no triangles to write")
            write(part)
            triangles_placed = len(part.faces)
            placements = 1
        else:
            components: list[tuple[SceneNode, _WrittenPart, int | None]] = []
            for node, mesh in placed:
                if _is_rigid(node.matrix):
                    done = written.get(node.mesh)
                    if done is None:
                        part = build(mesh, None, _part_name(mesh, node, used_names))
                        if not part.faces:
                            continue
                        done = write(part)
                        written[node.mesh] = done
                    components.append(
                        (node, done, _placement_axis(writer, node.matrix, factor))
                    )
                else:
                    part = build(mesh, node.matrix, _part_name(mesh, node, used_names))
                    if not part.faces:
                        continue
                    components.append((node, write(part), None))
                    baked += 1
                triangles_placed += components[-1][1].triangles
            if not components:
                raise StepExportError("the scene holds no triangles to write")
            axes = [
                axis if axis is not None else context.origin
                for _, _, axis in components
            ]
            placed_axes = _refs(sorted(set(axes) - {context.origin}))
            root_representation = writer.add(
                f"SHAPE_REPRESENTATION('',(#{context.origin},{placed_axes}),"
                f"#{context.representation_context})"
            )
            root = _product(
                writer,
                context,
                (name or "").strip() or _DEFAULT_ASSEMBLY_NAME,
                root_representation,
            )
            for position, (node, done, axis) in enumerate(components, start=1):
                target_axis = axis if axis is not None else context.origin
                transform = writer.add(
                    f"ITEM_DEFINED_TRANSFORMATION('','',#{context.origin},#{target_axis})"
                )
                relation = writer.add(
                    "( REPRESENTATION_RELATIONSHIP('','',"
                    f"#{done.representation},#{root_representation})"
                    f" REPRESENTATION_RELATIONSHIP_WITH_TRANSFORMATION(#{transform})"
                    " SHAPE_REPRESENTATION_RELATIONSHIP() )"
                )
                label = (node.name or "").strip() or f"{_DEFAULT_PART_NAME}-{position}"
                usage = writer.add(
                    f"NEXT_ASSEMBLY_USAGE_OCCURRENCE('{position}',{_text(label)},'',"
                    f"#{root},#{done.definition},$)"
                )
                shape = writer.add(
                    "PRODUCT_DEFINITION_SHAPE('Placement','Placement of an item',"
                    f"#{usage})"
                )
                writer.add(
                    f"CONTEXT_DEPENDENT_SHAPE_REPRESENTATION(#{relation},#{shape})"
                )
            placements = len(components)
        writer.flush()
        stream.write("ENDSEC;\nEND-ISO-10303-21;\n")
    return DirectStepResult(
        path=target,
        byte_length=target.stat().st_size,
        schema=schema,
        parts=len(names),
        placements=placements,
        baked_placements=baked,
        triangles_placed=triangles_placed,
        triangles_written=totals["triangles"],
        vertices_written=totals["vertices"],
        degenerate_triangles=totals["degenerate"],
        solids=totals["solids"],
        shells=totals["shells"],
        open_shells=totals["open_shells"],
        boundary_edges=totals["boundary_edges"],
        split_edges=totals["split_edges"],
        styled_faces=totals["styled_faces"],
        names=tuple(names),
        representation=representation,
        tessellated_faces=totals["tessellated_faces"],
        diagnostics=_diagnostics(
            parts=len(names),
            placements=placements,
            baked=baked,
            triangles=totals["triangles"],
            triangles_placed=triangles_placed,
            open_shells=totals["open_shells"],
            free_edges=totals["boundary_edges"] + totals["split_edges"],
            degenerate=totals["degenerate"],
            representation=representation,
            tessellated_faces=totals["tessellated_faces"],
        ),
    )


def _diagnostics(
    *,
    parts: int,
    placements: int,
    baked: int,
    triangles: int,
    triangles_placed: int,
    open_shells: int,
    free_edges: int,
    degenerate: int,
    representation: Representation = "brep",
    tessellated_faces: int = 0,
) -> list[ImportDiagnostic]:
    """The same codes :func:`cad3d_ir_step.export.mesh_to_step` reports."""
    details = {
        "triangles": triangles,
        "triangles_placed": triangles_placed,
        "bodies": parts,
        "placements": placements,
    }
    if representation == "tessellated":
        first = ImportDiagnostic(
            code="STEP_MESH_TESSELLATED",
            severity="info",
            action="approximated",
            message=(
                "The STEP carries the display mesh as AP242 tessellated geometry "
                "(TRIANGULATED_FACE). CAD that reads tessellated AP242 shows it as a "
                "mesh or graphics body; there are no B-rep faces to edit or measure."
            ),
            details={**details, "faces": tessellated_faces},
        )
    else:
        first = ImportDiagnostic(
            code="STEP_MESH_SOLID_FACETED",
            severity="info",
            action="approximated",
            message=(
                "The STEP was built from the display mesh: every face is a planar "
                "triangle. It opens in CAD that reads B-rep STEP but is not editable "
                "geometry (no cylinders, fillets or dimensions)."
            ),
            details=details,
        )
    diagnostics = [first]
    if open_shells:
        diagnostics.append(
            ImportDiagnostic(
                code="STEP_MESH_FREE_EDGES",
                severity="warning",
                action="unresolved",
                message=(
                    f"{open_shells} shell(s) did not close ({free_edges} free edges) "
                    "and were written as surface models instead of solids."
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
    if baked:
        diagnostics.append(
            ImportDiagnostic(
                code="STEP_MESH_PLACEMENTS_BAKED",
                severity="info",
                action="approximated",
                message=(
                    f"{baked} placement(s) mirror or scale their mesh; each was "
                    "written as its own part with the transform applied."
                ),
                details={"placements": baked},
            )
        )
    return diagnostics


DIRECT_BYTES_PER_TRIANGLE: Final = 800
"""Rough upper size of one written triangle with ``LINE`` edges (600–800 bytes)."""
TESSELLATED_BYTES_PER_TRIANGLE: Final = 24
"""Rough size of one triangle of a ``TRIANGULATED_FACE`` (three indices)."""
TESSELLATED_BYTES_PER_VERTEX: Final = 48
"""Rough size of one vertex: its coordinates and its ``pnindex`` entry."""
TESSELLATED_BYTES_PER_NORMAL: Final = 32
"""Rough size of one vertex normal."""


@dataclass(frozen=True, slots=True)
class MeshStepEstimate:
    """What :func:`write_mesh_step` would write, counted without writing."""

    triangles: int
    """Triangles written: each rigidly placed mesh once, baked placements each."""
    triangles_placed: int
    """Triangles once every placement is expanded (what some CAD loads)."""
    parts: int
    placements: int
    byte_length: int
    """B-rep: ``triangles`` × :data:`DIRECT_BYTES_PER_TRIANGLE`. Tessellated:
    the triangle, vertex and normal counts times their ``TESSELLATED_BYTES_*``."""
    vertices: int = 0
    """Vertices of the written meshes' vertex sets, before welding."""
    representation: Representation = "brep"


def estimate_mesh_step(
    scene: MeshScene,
    *,
    representation: Representation = "brep",
    normals: bool = True,
) -> MeshStepEstimate:
    """Count what :func:`write_mesh_step` would write for ``scene``.

    Degenerate triangles and vertices welded away are still counted, so the
    estimate errs high.
    """
    placed = list(_placements(scene))
    seen: set[int | None] = set()
    triangles = triangles_placed = parts = vertices = 0
    for node, mesh in placed:
        count = sum(
            len(primitive.indices) // 3
            for primitive in mesh.primitives
            if primitive.mode == MODE_TRIANGLES
        )
        triangles_placed += count
        if len(placed) > 1 and _is_rigid(node.matrix):
            if node.mesh in seen:
                continue
            seen.add(node.mesh)
        triangles += count
        parts += 1
        sets = {
            primitive.vertex_set: len(primitive.positions) // 3
            for primitive in mesh.primitives
            if primitive.mode == MODE_TRIANGLES and primitive.indices
        }
        vertices += sum(sets.values())
    if representation == "tessellated":
        per_vertex = TESSELLATED_BYTES_PER_VERTEX + (
            TESSELLATED_BYTES_PER_NORMAL if normals else 0
        )
        byte_length = triangles * TESSELLATED_BYTES_PER_TRIANGLE + vertices * per_vertex
    else:
        byte_length = triangles * DIRECT_BYTES_PER_TRIANGLE
    return MeshStepEstimate(
        triangles=triangles,
        triangles_placed=triangles_placed,
        parts=parts,
        placements=len(placed),
        byte_length=byte_length,
        vertices=vertices,
        representation=representation,
    )


__all__ = [
    "DIRECT_BYTES_PER_TRIANGLE",
    "TESSELLATED_BYTES_PER_NORMAL",
    "TESSELLATED_BYTES_PER_TRIANGLE",
    "TESSELLATED_BYTES_PER_VERTEX",
    "DirectSchema",
    "DirectStepResult",
    "EdgeCurve",
    "MeshStepEstimate",
    "Representation",
    "estimate_mesh_step",
    "write_mesh_step",
]
