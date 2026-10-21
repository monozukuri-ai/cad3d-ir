"""Bounded OCCT tessellation of XDE shapes into flat vertex arrays.

The output is kernel-independent data (positions, normals, indices, and edge
polylines in the shape's own coordinates) that :class:`cad3d_ir.mesh.GlbBuilder`
can write without touching OCCT again. Face and edge ``pick_id`` values are
1-based indices in ``TopExp::MapShapes`` order and are only meaningful together
with the tessellation that produced them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import sqrt
from typing import Any

from cad3d_ir.mesh.geometry import Bounds, extend_bounds
from OCP.Bnd import Bnd_Box
from OCP.BRep import BRep_Tool
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepLib import BRepLib_ToolTriangulatedShape
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.gp import gp_TrsfForm
from OCP.IMeshTools import IMeshTools_Parameters
from OCP.Precision import Precision
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_REVERSED
from OCP.TopExp import TopExp
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS
from OCP.TopTools import (
    TopTools_IndexedDataMapOfShapeListOfShape,
    TopTools_IndexedMapOfShape,
)


@dataclass(slots=True)
class FaceMesh:
    """Triangulation of one face, already transformed into shape coordinates."""

    pick_id: int
    positions: list[float]
    normals: list[float]
    indices: list[int]

    @property
    def triangle_count(self) -> int:
        return len(self.indices) // 3


@dataclass(slots=True)
class EdgePolyline:
    """Polyline of one edge sampled on the triangulation of an adjacent face."""

    pick_id: int
    positions: list[float]

    @property
    def segment_count(self) -> int:
        return len(self.positions) // 3 - 1


@dataclass(slots=True)
class ShapeMesh:
    """Tessellation result for one shape, plus what could not be meshed."""

    faces: list[FaceMesh] = field(default_factory=list)
    edges: list[EdgePolyline] = field(default_factory=list)
    face_count: int = 0
    edge_count: int = 0
    failed_faces: int = 0
    failed_edges: int = 0
    bounds: Bounds | None = None

    @property
    def triangle_count(self) -> int:
        return sum(face.triangle_count for face in self.faces)

    @property
    def vertex_count(self) -> int:
        return sum(len(face.positions) // 3 for face in self.faces) + sum(
            len(edge.positions) // 3 for edge in self.edges
        )

    @property
    def line_segment_count(self) -> int:
        return sum(edge.segment_count for edge in self.edges)


def shape_diagonal(shape: Any) -> float | None:
    """Return the diagonal of the shape's conservative bounding box, or ``None``.

    ``BRepBndLib::Add`` (surface control points) is used instead of the optimal
    box: the optimal variant took two minutes on a 47 MB industrial robot
    assembly and the deflection only needs the order of magnitude.
    """
    bounds = Bnd_Box()
    BRepBndLib.Add_s(shape, bounds, False)
    if bounds.IsVoid():
        return None
    xmin, ymin, zmin, xmax, ymax, zmax = bounds.Get()
    return sqrt((xmax - xmin) ** 2 + (ymax - ymin) ** 2 + (zmax - zmin) ** 2)


def linear_deflection_for(
    shape: Any,
    *,
    ratio: float,
    minimum: float | None = None,
) -> float:
    """Return an absolute linear deflection from a ratio of the shape diagonal."""
    floor = Precision.Confusion_s() if minimum is None else minimum
    diagonal = shape_diagonal(shape)
    if diagonal is None or diagonal <= 0.0:
        return max(floor, ratio)
    return max(floor, diagonal * ratio)


def mesh_shape(
    shape: Any,
    *,
    linear_deflection: float,
    angular_deflection: float,
    include_edges: bool = True,
    parallel: bool = True,
) -> ShapeMesh:
    """Mesh ``shape`` incrementally and extract faces and edges.

    Meshing is idempotent per underlying ``TShape``: calling this twice, or
    on a shape whose definition is shared by several occurrences, reuses the
    stored triangulation unless the deflection is tighter.
    """
    parameters = IMeshTools_Parameters()
    parameters.Deflection = float(linear_deflection)
    parameters.Angle = float(angular_deflection)
    parameters.AdjustMinSize = True
    parameters.InParallel = bool(parallel)
    BRepMesh_IncrementalMesh(shape, parameters)

    result = ShapeMesh()
    faces_by_edge = TopTools_IndexedDataMapOfShapeListOfShape()
    if include_edges:
        TopExp.MapShapesAndAncestors_s(shape, TopAbs_EDGE, TopAbs_FACE, faces_by_edge)

    face_map = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_FACE, face_map)
    result.face_count = face_map.Extent()
    for pick_id in range(1, face_map.Extent() + 1):
        face = TopoDS.Face_s(face_map.FindKey(pick_id))
        location = TopLoc_Location()
        triangulation = BRep_Tool.Triangulation_s(face, location)
        if (
            triangulation is None
            or triangulation.NbNodes() <= 0
            or triangulation.NbTriangles() <= 0
        ):
            result.failed_faces += 1
            continue
        if not triangulation.HasNormals():
            BRepLib_ToolTriangulatedShape.ComputeNormals_s(face, triangulation)
        transformation = location.Transformation()
        identity = transformation.Form() == gp_TrsfForm.gp_Identity
        node_count = triangulation.NbNodes()
        positions: list[float] = []
        normals: list[float] = []
        reversed_face = face.Orientation() == TopAbs_REVERSED
        for index in range(1, node_count + 1):
            point = triangulation.Node(index)
            normal = triangulation.Normal(index)
            if not identity:
                point = point.Transformed(transformation)
                normal = normal.Transformed(transformation)
            positions.extend((point.X(), point.Y(), point.Z()))
            if reversed_face:
                normals.extend((-normal.X(), -normal.Y(), -normal.Z()))
            else:
                normals.extend((normal.X(), normal.Y(), normal.Z()))
        indices: list[int] = []
        for index in range(1, triangulation.NbTriangles() + 1):
            first, second, third = triangulation.Triangle(index).Get()
            if reversed_face:
                indices.extend((first - 1, third - 1, second - 1))
            else:
                indices.extend((first - 1, second - 1, third - 1))
        result.faces.append(
            FaceMesh(
                pick_id=pick_id, positions=positions, normals=normals, indices=indices
            )
        )
        result.bounds = extend_bounds(result.bounds, positions)

    if include_edges:
        edge_map = TopTools_IndexedMapOfShape()
        TopExp.MapShapes_s(shape, TopAbs_EDGE, edge_map)
        result.edge_count = edge_map.Extent()
        for pick_id in range(1, edge_map.Extent() + 1):
            edge = TopoDS.Edge_s(edge_map.FindKey(pick_id))
            polyline = _edge_polyline(edge, faces_by_edge)
            if polyline is None:
                result.failed_edges += 1
                continue
            result.edges.append(EdgePolyline(pick_id=pick_id, positions=polyline))
            result.bounds = extend_bounds(result.bounds, polyline)
    return result


def _edge_polyline(edge: Any, faces_by_edge: Any) -> list[float] | None:
    if not faces_by_edge.Contains(edge):
        return None
    face = TopoDS.Face_s(faces_by_edge.FindFromKey(edge).First())
    location = TopLoc_Location()
    triangulation = BRep_Tool.Triangulation_s(face, location)
    if triangulation is None:
        return None
    polygon = BRep_Tool.PolygonOnTriangulation_s(edge, triangulation, location)
    if polygon is None or polygon.NbNodes() < 2:
        return None
    transformation = location.Transformation()
    identity = transformation.Form() == gp_TrsfForm.gp_Identity
    positions: list[float] = []
    for index in range(1, polygon.NbNodes() + 1):
        point = triangulation.Node(polygon.Node(index))
        if not identity:
            point = point.Transformed(transformation)
        positions.extend((point.X(), point.Y(), point.Z()))
    return positions


__all__ = [
    "EdgePolyline",
    "FaceMesh",
    "ShapeMesh",
    "linear_deflection_for",
    "mesh_shape",
    "shape_diagonal",
]
