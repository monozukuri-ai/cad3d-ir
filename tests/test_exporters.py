from __future__ import annotations

import math
import struct
from pathlib import Path

import pytest

from cad3d_ir import DirectoryResourceWriter
from cad3d_ir.cli import main
from cad3d_ir.exporters import (
    OBJ_MEDIA_TYPE,
    STL_MEDIA_TYPE,
    estimate_obj_bytes,
    estimate_stl_bytes,
    write_obj,
    write_stl,
)
from cad3d_ir.importers import ImportOptions, MeshImporter
from cad3d_ir.importers.mesh import read_obj, read_stl
from cad3d_ir.mesh import GlbBuilder, flat_normals, read_glb, vertex_normals


def _records(data: bytes, prefix: str) -> list[str]:
    return [
        line for line in data.decode("utf-8").splitlines() if line.startswith(prefix)
    ]


def test_write_stl_binary_round_trip(tmp_path: Path, cubes_glb: bytes) -> None:
    export = write_stl(cubes_glb)

    assert export.format == "stl" and export.suffix == ".stl"
    assert export.media_type == STL_MEDIA_TYPE
    assert export.triangles == 24 and export.vertices == 72
    assert export.degenerate_triangles == 0 and export.placements == 2
    assert export.bounds == ((0.0, 0.0, 0.0), (30.0, 10.0, 10.0))
    assert export.byte_length == estimate_stl_bytes(24) == 84 + 50 * 24
    assert not export.data[:5].lower().startswith(b"solid")
    assert struct.unpack_from("<I", export.data, 80)[0] == 24
    # The first triangle is the bottom face (z = 0), so its normal points down.
    normal = struct.unpack_from("<3f", export.data, 84)
    assert normal == (0.0, 0.0, -1.0)
    path = tmp_path / "cubes.stl"
    path.write_bytes(export.data)
    data = read_stl(path)
    assert data.triangle_count == 24 and not data.binary_length_mismatch
    assert min(data.triangles[0::3]) == 0.0 and max(data.triangles[0::3]) == 30.0


def test_write_stl_ascii_round_trip(tmp_path: Path, cubes_glb: bytes) -> None:
    export = write_stl(cubes_glb, binary=False, name="My Part")

    text = export.data.decode("utf-8")
    assert text.startswith("solid My_Part\n")
    assert text.endswith("endsolid My_Part\n")
    assert text.count("facet normal") == 24 and export.triangles == 24
    assert "      vertex 30 10 10" in text
    path = tmp_path / "cubes.stl"
    path.write_bytes(export.data)
    assert read_stl(path).triangle_count == 24
    with pytest.raises(ValueError, match="solid"):
        write_stl(cubes_glb, header="solid trap")


def test_write_obj_round_trip_and_layout(tmp_path: Path, cubes_glb: bytes) -> None:
    export = write_obj(cubes_glb)

    assert export.format == "obj" and export.media_type == OBJ_MEDIA_TYPE
    assert export.triangles == 24 and export.vertices == 16
    assert export.placements == 2 and export.line_segments == 0
    assert export.bounds == ((0.0, 0.0, 0.0), (30.0, 10.0, 10.0))
    lines = export.data.decode("utf-8").splitlines()
    assert lines[0].startswith("# cad3d-ir ") and lines[1] == "# units: millimetres"
    assert _records(export.data, "o ") == ["o cube-a", "o cube-b"]
    assert len(_records(export.data, "v ")) == 16
    assert len(_records(export.data, "vn ")) == 16
    faces = _records(export.data, "f ")
    assert len(faces) == 24 and all("//" in face for face in faces)
    assert not _records(export.data, "l ")
    # The second placement's faces index its own vertices (9..16) and normals.
    second = faces[12:]
    used = {int(token.split("//")[0]) for face in second for token in face.split()[1:]}
    assert used == set(range(9, 17))
    assert faces[12].split()[1].split("//") == ["9", "9"]
    assert "v 20 0 0" in lines
    path = tmp_path / "cubes.obj"
    path.write_bytes(export.data)
    data = read_obj(path)
    assert data.triangle_count == 24 and data.groups_present
    assert data.polylines == [] and not data.materials_present


def test_write_obj_options(tmp_path: Path, cubes_glb: bytes) -> None:
    plain = write_obj(cubes_glb, normals=False, groups=False)
    assert not _records(plain.data, "vn ") and not _records(plain.data, "o ")
    faces = _records(plain.data, "f ")
    assert faces[0] == "f 1 4 3" and "//" not in faces[-1]
    assert plain.placements == 2

    wired = write_obj(cubes_glb, lines=True, comment="made by a test\nsecond line")
    assert len(_records(wired.data, "l ")) == 24 and wired.line_segments == 24
    assert "# made by a test" in wired.data.decode("utf-8")
    assert "# second line" in wired.data.decode("utf-8")
    path = tmp_path / "wired.obj"
    path.write_bytes(wired.data)
    data = read_obj(path)
    assert data.triangle_count == 24 and len(data.polylines) == 24


def test_degenerate_triangles_are_dropped_in_both_formats(
    tmp_path: Path, degenerate_cubes_glb: bytes
) -> None:
    scene = read_glb(degenerate_cubes_glb)
    assert scene.placed_triangle_count == 26

    stl = write_stl(scene)
    obj = write_obj(scene)

    assert stl.triangles == 24 and stl.degenerate_triangles == 2
    assert obj.triangles == 24 and obj.degenerate_triangles == 2
    (tmp_path / "d.stl").write_bytes(stl.data)
    (tmp_path / "d.obj").write_bytes(obj.data)
    assert read_stl(tmp_path / "d.stl").triangle_count == 24
    assert read_obj(tmp_path / "d.obj").triangle_count == 24


def test_mesh_importer_glb_exports_the_imported_triangles(
    tmp_path: Path, cubes_glb: bytes
) -> None:
    source = tmp_path / "cubes.stl"
    source.write_bytes(write_stl(cubes_glb, binary=False).data)
    result = MeshImporter().import_file(
        source,
        options=ImportOptions(),
        resources=DirectoryResourceWriter(tmp_path / "package"),
    )
    glb_path = next((tmp_path / "package").rglob("*.glb"))

    obj = write_obj(glb_path.read_bytes())
    stl = write_stl(glb_path.read_bytes())

    assert result.statistics["triangles"] == 24
    assert obj.triangles == 24 and obj.vertices == 72  # de-indexed by the importer
    assert stl.triangles == 24
    (tmp_path / "again.obj").write_bytes(obj.data)
    assert read_obj(tmp_path / "again.obj").triangle_count == 24
    assert obj.bounds == stl.bounds == ((0.0, 0.0, 0.0), (30.0, 10.0, 10.0))


def _grid_glb(size: int, *, shared: bool) -> bytes:
    """A wavy ``size`` x ``size`` quad grid, with a shared or de-indexed vertex set."""
    positions: list[float] = []
    for row in range(size + 1):
        for column in range(size + 1):
            x = column * 12.5
            y = row * 12.5
            positions.extend((x, y, 40.0 * math.sin(x / 50.0) * math.cos(y / 70.0)))
    indices: list[int] = []
    for row in range(size):
        for column in range(size):
            a = row * (size + 1) + column
            b = a + 1
            c = a + size + 1
            d = c + 1
            indices.extend((a, b, d, a, d, c))
    builder = GlbBuilder(root_name="grid")
    mesh = builder.add_mesh("grid")
    if shared:
        builder.add_triangles(
            mesh,
            positions=positions,
            normals=vertex_normals(positions, indices),
            indices=indices,
        )
    else:
        soup: list[float] = []
        for index in indices:
            soup.extend(positions[index * 3 : index * 3 + 3])
        builder.add_triangles(
            mesh,
            positions=soup,
            normals=flat_normals(soup),
            indices=list(range(len(indices))),
        )
    builder.add_node("grid", mesh=mesh)
    return builder.build()


@pytest.mark.parametrize("shared", [True, False])
@pytest.mark.parametrize("normals", [True, False])
def test_estimates_track_written_sizes(shared: bool, normals: bool) -> None:
    scene = read_glb(_grid_glb(60, shared=shared))
    assert scene.placed_triangle_count == 7200
    assert scene.placed_vertex_count == (3721 if shared else 21600)

    obj = write_obj(scene, normals=normals)
    estimate = estimate_obj_bytes(
        scene.placed_vertex_count, scene.placed_triangle_count, normals=normals
    )
    assert abs(estimate - obj.byte_length) / obj.byte_length < 0.15, (
        estimate,
        obj.byte_length,
    )

    stl = write_stl(scene)
    assert stl.byte_length == estimate_stl_bytes(7200)
    ascii_stl = write_stl(scene, binary=False)
    ascii_estimate = estimate_stl_bytes(7200, binary=False)
    assert abs(ascii_estimate - ascii_stl.byte_length) / ascii_stl.byte_length < 0.25


def test_estimates_reject_negative_counts() -> None:
    with pytest.raises(ValueError):
        estimate_stl_bytes(-1)
    with pytest.raises(ValueError):
        estimate_obj_bytes(1, -1)
    assert estimate_obj_bytes(0, 0, groups=0) > 0


def test_write_rejects_unknown_sources() -> None:
    with pytest.raises(TypeError):
        write_stl(123)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        write_obj("part.glb")  # type: ignore[arg-type]


def test_cli_export_writes_files(tmp_path: Path, cubes_glb: bytes, capsys) -> None:
    glb = tmp_path / "cubes.glb"
    glb.write_bytes(cubes_glb)
    stl = tmp_path / "out" / "cubes.stl"
    obj = tmp_path / "out" / "cubes.obj"

    result = main(
        [
            "export",
            str(glb),
            "--stl",
            str(stl),
            "--obj",
            str(obj),
            "--lines",
            "--no-groups",
        ]
    )

    assert result == 0
    out = capsys.readouterr().out
    assert f"wrote {stl}: 24 triangles, 72 vertices" in out
    assert f"wrote {obj}: 24 triangles, 16 vertices" in out
    assert read_stl(stl).triangle_count == 24
    data = read_obj(obj)
    assert data.triangle_count == 24 and len(data.polylines) == 24
    assert not data.groups_present

    assert main(["export", str(glb), "--ascii-stl", "--stl", str(stl)]) == 0
    assert stl.read_bytes().startswith(b"solid ")

    assert main(["export", str(glb)]) == 2
    assert "--stl and/or --obj" in capsys.readouterr().err

    bad = tmp_path / "bad.glb"
    bad.write_bytes(b"not a glb at all, really")
    assert main(["export", str(bad), "--stl", str(stl)]) == 1
    assert "error:" in capsys.readouterr().err
