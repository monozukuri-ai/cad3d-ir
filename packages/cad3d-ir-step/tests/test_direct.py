from __future__ import annotations

from pathlib import Path

import pytest
from cad3d_ir.mesh import GlbBuilder, read_glb, vertex_normals
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepGProp import BRepGProp
from OCP.GProp import GProp_GProps
from OCP.IFSelect import IFSelect_RetDone
from OCP.STEPControl import STEPControl_Reader
from OCP.TopAbs import TopAbs_FACE, TopAbs_SHELL, TopAbs_SOLID
from OCP.TopExp import TopExp_Explorer

from cad3d_ir_step import (
    DIRECT_BYTES_PER_TRIANGLE,
    StepExportError,
    estimate_mesh_step,
    write_mesh_step,
)
from cad3d_ir_step.export_cli import main as export_main

CUBE_POSITIONS = [
    0.0, 0.0, 0.0, 10.0, 0.0, 0.0, 10.0, 10.0, 0.0, 0.0, 10.0, 0.0,
    0.0, 0.0, 10.0, 10.0, 0.0, 10.0, 10.0, 10.0, 10.0, 0.0, 10.0, 10.0,
]  # fmt: skip
CUBE_INDICES = [
    0, 3, 2, 0, 2, 1, 4, 5, 6, 4, 6, 7, 0, 1, 5, 0, 5, 4,
    1, 2, 6, 1, 6, 5, 2, 3, 7, 2, 7, 6, 3, 0, 4, 3, 4, 7,
]  # fmt: skip
RED = (0.9, 0.2, 0.2, 1.0)
BLUE = (0.2, 0.3, 0.9, 1.0)


def _translate(dx: float, *, mirror: bool = False) -> tuple[tuple[float, ...], ...]:
    return (
        (-1.0 if mirror else 1.0, 0.0, 0.0, dx),
        (0.0, 1.0, 0.0, 0.0),
        (0.0, 0.0, 1.0, 0.0),
        (0.0, 0.0, 0.0, 1.0),
    )


def _scene(
    meshes: list[tuple[str, list[float], list[int]]],
    nodes: list[tuple[str, int, tuple[tuple[float, ...], ...]]],
    *,
    split_faces: bool = False,
):
    """``meshes``: name, positions, indices; ``nodes``: name, mesh index, matrix."""
    return read_glb(_glb(meshes, nodes, split_faces=split_faces))


def _glb(
    meshes: list[tuple[str, list[float], list[int]]],
    nodes: list[tuple[str, int, tuple[tuple[float, ...], ...]]],
    *,
    split_faces: bool = False,
) -> bytes:
    builder = GlbBuilder(root_name="root")
    red = builder.add_material("red", RED)
    blue = builder.add_material("blue", BLUE)
    built = []
    for name, positions, indices in meshes:
        mesh = builder.add_mesh(name)
        if split_faces:
            # one vertex set per pair of triangles, as CAD writers tessellate per face
            for start in range(0, len(indices), 6):
                chunk = indices[start : start + 6]
                used = sorted(set(chunk))
                local = [
                    value
                    for vertex in used
                    for value in positions[vertex * 3 : vertex * 3 + 3]
                ]
                remap = {vertex: i for i, vertex in enumerate(used)}
                vertices = builder.add_vertices(
                    positions=local,
                    normals=vertex_normals(local, [remap[v] for v in chunk]),
                )
                builder.add_triangles(
                    mesh,
                    indices=[remap[v] for v in chunk],
                    vertices=vertices,
                    material=blue if start == 0 else red,
                )
        else:
            vertices = builder.add_vertices(
                positions=positions, normals=vertex_normals(positions, indices)
            )
            builder.add_triangles(
                mesh, indices=indices, vertices=vertices, material=red
            )
        built.append(mesh)
    for name, mesh_index, matrix in nodes:
        builder.add_node(name, mesh=built[mesh_index], matrix=matrix)
    return builder.build()


def _occt(path: Path) -> tuple[int, int, int, float, bool]:
    reader = STEPControl_Reader()
    assert reader.ReadFile(str(path)) == IFSelect_RetDone
    reader.TransferRoots()
    shape = reader.OneShape()

    def count(kind) -> int:
        explorer, total = TopExp_Explorer(shape, kind), 0
        while explorer.More():
            total += 1
            explorer.Next()
        return total

    volume = 0.0
    explorer = TopExp_Explorer(shape, TopAbs_SOLID)
    while explorer.More():
        properties = GProp_GProps()
        BRepGProp.VolumeProperties_s(explorer.Current(), properties)
        volume += properties.Mass()
        explorer.Next()
    valid = BRepCheck_Analyzer(shape).IsValid()
    return count(TopAbs_FACE), count(TopAbs_SHELL), count(TopAbs_SOLID), volume, valid


def test_cubes_welded_from_face_patches_become_one_solid(tmp_path: Path) -> None:
    scene = _scene(
        [("cube", CUBE_POSITIONS, CUBE_INDICES)],
        [("cube", 0, _translate(5.0))],
        split_faces=True,
    )
    result = write_mesh_step(scene, tmp_path / "cube.step", name="cube")
    assert (result.parts, result.placements, result.solids, result.open_shells) == (
        1,
        1,
        1,
        0,
    )
    assert result.triangles_written == 12
    assert result.styled_faces == 2  # the first face pair is blue, the rest red
    faces, shells, solids, volume, valid = _occt(result.path)
    assert (faces, shells, solids, valid) == (12, 1, 1, True)
    assert volume == pytest.approx(1000.0)
    text = result.path.read_text(encoding="ascii")
    assert "AUTOMOTIVE_DESIGN" in text
    assert "MANIFOLD_SOLID_BREP" in text and "SURFACE_CURVE" not in text


def test_a_mesh_placed_twice_is_written_once(tmp_path: Path) -> None:
    scene = _scene(
        [("cube", CUBE_POSITIONS, CUBE_INDICES)],
        [("a", 0, _translate(0.0)), ("b", 0, _translate(20.0))],
    )
    result = write_mesh_step(scene, tmp_path / "pair.step", name="pair")
    assert (result.parts, result.placements, result.baked_placements) == (1, 2, 0)
    assert (result.triangles_written, result.triangles_placed) == (12, 24)
    text = result.path.read_text(encoding="ascii")
    assert text.count("NEXT_ASSEMBLY_USAGE_OCCURRENCE") == 2
    assert text.count("ADVANCED_FACE") == 12
    faces, _, solids, volume, valid = _occt(result.path)
    assert (faces, solids, valid) == (24, 2, True)
    assert volume == pytest.approx(2000.0)


def test_a_mirrored_placement_gets_its_own_outward_solid(tmp_path: Path) -> None:
    scene = _scene(
        [("cube", CUBE_POSITIONS, CUBE_INDICES)],
        [("a", 0, _translate(0.0)), ("m", 0, _translate(30.0, mirror=True))],
    )
    result = write_mesh_step(scene, tmp_path / "mirror.step")
    assert (result.parts, result.placements, result.baked_placements) == (2, 2, 1)
    _, _, solids, volume, valid = _occt(result.path)
    # the mirror reverses the winding: the baked part is reoriented outwards
    assert (solids, valid) == (2, True)
    assert volume == pytest.approx(2000.0)


def test_inconsistent_winding_is_reoriented_into_a_solid(tmp_path: Path) -> None:
    indices = list(CUBE_INDICES)
    for start in (0, 9, 21):  # flip three triangles
        indices[start + 1], indices[start + 2] = indices[start + 2], indices[start + 1]
    scene = _scene([("cube", CUBE_POSITIONS, indices)], [("cube", 0, _translate(0.0))])
    result = write_mesh_step(scene, tmp_path / "flipped.step")
    assert (result.solids, result.open_shells, result.split_edges) == (1, 0, 0)
    _, _, solids, volume, valid = _occt(result.path)
    assert (solids, valid) == (1, True)
    assert volume == pytest.approx(1000.0)


def test_an_open_mesh_is_a_surface_model(tmp_path: Path) -> None:
    scene = _scene(
        [("tray", CUBE_POSITIONS, CUBE_INDICES[6:])],  # the bottom face is missing
        [("tray", 0, _translate(0.0))],
    )
    result = write_mesh_step(scene, tmp_path / "tray.step")
    assert (result.solids, result.shells, result.open_shells) == (0, 1, 1)
    assert result.boundary_edges == 4
    text = result.path.read_text(encoding="ascii")
    assert "SHELL_BASED_SURFACE_MODEL" in text and "OPEN_SHELL" in text
    faces, shells, solids, _, valid = _occt(result.path)
    assert (faces, shells, solids, valid) == (10, 1, 0, True)


def test_ap242_polyline_edges_and_names(tmp_path: Path) -> None:
    scene = _scene(
        [("部品 A", CUBE_POSITIONS, CUBE_INDICES)],
        [("部品 A:1", 0, _translate(0.0)), ("it's B", 0, _translate(20.0))],
    )
    result = write_mesh_step(
        scene,
        tmp_path / "named.step",
        schema="AP242",
        name="組図",
        edge_curve="polyline",
    )
    text = result.path.read_text(encoding="ascii")
    assert "AP242_MANAGED_MODEL_BASED_3D_ENGINEERING_MIM_LF" in text
    assert "=POLYLINE(" in text and "=LINE(" not in text
    assert "'it''s B'" in text
    assert "\\X2\\90E854C1\\X0\\ A" in text  # 部品 A
    assert result.names == ("部品 A",)
    faces, _, solids, _, valid = _occt(result.path)
    assert (faces, solids, valid) == (24, 2, True)


def test_rejects_bad_options_and_empty_scenes(tmp_path: Path) -> None:
    scene = _scene(
        [("cube", CUBE_POSITIONS, CUBE_INDICES)], [("cube", 0, _translate(0.0))]
    )
    with pytest.raises(ValueError, match="schema"):
        write_mesh_step(scene, tmp_path / "x.step", schema="AP203")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="edge_curve"):
        write_mesh_step(scene, tmp_path / "x.step", edge_curve="spline")  # type: ignore[arg-type]
    degenerate = _scene(
        [("flat", [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 2.0, 0.0, 0.0], [0, 1, 2])],
        [("flat", 0, _translate(0.0))],
    )
    with pytest.raises(StepExportError, match="no triangles"):
        write_mesh_step(degenerate, tmp_path / "x.step")


def test_estimate_counts_each_rigid_mesh_once() -> None:
    cube = [("cube", CUBE_POSITIONS, CUBE_INDICES)]
    pair = estimate_mesh_step(
        _scene(cube, [("a", 0, _translate(0.0)), ("b", 0, _translate(20.0))])
    )
    assert (pair.triangles, pair.triangles_placed, pair.parts, pair.placements) == (
        12,
        24,
        1,
        2,
    )
    assert pair.byte_length == 12 * DIRECT_BYTES_PER_TRIANGLE
    mirrored = estimate_mesh_step(
        _scene(
            cube, [("a", 0, _translate(0.0)), ("m", 0, _translate(30.0, mirror=True))]
        )
    )
    assert (mirrored.triangles, mirrored.parts) == (24, 2)


def test_results_carry_the_mesh_step_diagnostics(tmp_path: Path) -> None:
    tray = _scene(
        [("tray", CUBE_POSITIONS, CUBE_INDICES[6:])],
        [("a", 0, _translate(0.0)), ("m", 0, _translate(30.0, mirror=True))],
    )
    result = write_mesh_step(tray, tmp_path / "tray.step")
    codes = {item.code: item.details for item in result.diagnostics}
    assert codes["STEP_MESH_SOLID_FACETED"]["triangles"] == 20
    assert codes["STEP_MESH_SOLID_FACETED"]["triangles_placed"] == 20
    assert codes["STEP_MESH_FREE_EDGES"] == {"open_shells": 2, "free_edges": 8}
    assert codes["STEP_MESH_PLACEMENTS_BAKED"] == {"placements": 1}


def test_cli_direct_mode(tmp_path: Path, capsys) -> None:
    glb = tmp_path / "pair.glb"
    glb.write_bytes(
        _glb(
            [("cube", CUBE_POSITIONS, CUBE_INDICES)],
            [("a", 0, _translate(0.0)), ("b", 0, _translate(20.0))],
        )
    )
    out = tmp_path / "pair.step"
    report = tmp_path / "pair.json"
    args = [str(glb), str(out), "--mode", "direct", "--diagnostics", str(report)]
    assert export_main(args) == 0
    assert "1 parts, 2 placements, 12 triangles (24 placed)" in capsys.readouterr().out
    assert '"mode": "direct"' in report.read_text(encoding="utf-8")
    assert export_main([*args, "--max-triangles", "10"]) == 3
    assert export_main([*args, "--schema", "AP203"]) == 1
    # the cap of the sewn modes still applies by default
    assert export_main([str(glb), str(tmp_path / "sewn.step")]) == 0
