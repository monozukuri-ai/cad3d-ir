"""Meshing of shapes with free edges (wireframe IGES drawings, construction lines)."""

from __future__ import annotations

from pathlib import Path

import pytest
from cad3d_ir import DirectoryResourceWriter, validate_package
from cad3d_ir.importers import ImportOptions
from cad3d_ir.mesh import validate_glb_bytes
from cad3d_ir.mesh.scene import MODE_TRIANGLES, read_glb
from OCP.BRep import BRep_Builder
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
from OCP.gp import gp_Ax2, gp_Circ, gp_Dir, gp_Pnt
from OCP.IGESControl import IGESControl_Controller, IGESControl_Writer
from OCP.TopoDS import TopoDS_Compound

from cad3d_ir_step import IgesImporter
from cad3d_ir_step.tessellate import _free_edge_polyline, mesh_shape


def _box_with_free_edges() -> TopoDS_Compound:
    """A box next to a free line (x 20..60) and a free circle (y up to 35)."""
    builder = BRep_Builder()
    compound = TopoDS_Compound()
    builder.MakeCompound(compound)
    builder.Add(compound, BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape())
    line = BRepBuilderAPI_MakeEdge(gp_Pnt(20.0, 0.0, 0.0), gp_Pnt(60.0, 0.0, 0.0))
    builder.Add(compound, line.Edge())
    circle = gp_Circ(gp_Ax2(gp_Pnt(40.0, 30.0, 0.0), gp_Dir(0.0, 0.0, 1.0)), 5.0)
    builder.Add(compound, BRepBuilderAPI_MakeEdge(circle).Edge())
    return compound


def test_mesh_shape_keeps_free_edges() -> None:
    mesh = mesh_shape(
        _box_with_free_edges(), linear_deflection=0.1, angular_deflection=0.3
    )
    assert mesh.face_count == 6 and mesh.failed_faces == 0
    # 12 box edges + the free line + the free circle, none skipped
    assert mesh.edge_count == 14 and mesh.failed_edges == 0
    assert len(mesh.edges) == 14
    assert mesh.bounds is not None
    assert mesh.bounds[1][0] == pytest.approx(60.0)
    assert mesh.bounds[1][1] == pytest.approx(35.0, abs=0.1)  # chords of the circle
    assert max(len(edge.positions) // 3 for edge in mesh.edges) > 2


def test_free_edge_without_stored_polygon_is_discretised() -> None:
    circle = gp_Circ(gp_Ax2(gp_Pnt(0.0, 0.0, 0.0), gp_Dir(0.0, 0.0, 1.0)), 5.0)
    edge = BRepBuilderAPI_MakeEdge(circle).Edge()  # never meshed: no Polygon3D
    positions = _free_edge_polyline(edge, 0.1, 0.3)
    assert positions is not None and len(positions) // 3 >= 8
    line = BRepBuilderAPI_MakeEdge(gp_Pnt(0.0, 0.0, 0.0), gp_Pnt(3.0, 0.0, 0.0))
    assert _free_edge_polyline(line.Edge(), 0.1, 0.3) == pytest.approx(
        [0.0, 0.0, 0.0, 3.0, 0.0, 0.0]
    )


def test_wireframe_iges_imports_with_its_edges(tmp_path: Path) -> None:
    IGESControl_Controller.Init_s()
    writer = IGESControl_Writer("MM", 0)
    assert writer.AddShape(_box_with_free_edges())
    writer.ComputeModel()
    path = tmp_path / "wireframe.igs"
    assert writer.Write(str(path))
    root = tmp_path / "out"
    result = IgesImporter().import_file(
        path,
        options=ImportOptions(include_visualization_mesh=True, mesh_include_edges=True),
        resources=DirectoryResourceWriter(root),
    )
    package = result.package
    validate_package(package, resource_root=root)
    preview_uri = next(
        item.uri
        for item in package.resources
        if item.id == package.visualizations[0].resource_id
    )
    payload = (root / preview_uri).read_bytes()
    report = validate_glb_bytes(payload)
    assert report.valid, report.errors
    scene = read_glb(payload)
    lines = [
        primitive
        for mesh in scene.meshes
        for primitive in mesh.primitives
        if primitive.mode != MODE_TRIANGLES
    ]
    assert lines, "the free line and circle must reach the viewer"
    bounds = package.visualizations[0].bounds
    assert bounds is not None
    assert bounds.max[0] == pytest.approx(60.0, abs=1e-3)
    assert bounds.max[1] == pytest.approx(35.0, abs=0.1)
