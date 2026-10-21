from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from cad3d_ir import DirectoryResourceWriter, load_manifest, validate_package
from cad3d_ir.cli import main
from cad3d_ir.importers import ImportOptions, MeshImporter, MeshImportError
from cad3d_ir.importers.base import Importer
from cad3d_ir.importers.mesh import detect_mesh_format, read_obj, read_stl
from cad3d_ir.mesh import validate_glb_bytes

_CUBE_VERTICES = [
    (0.0, 0.0, 0.0),
    (10.0, 0.0, 0.0),
    (10.0, 10.0, 0.0),
    (0.0, 10.0, 0.0),
    (0.0, 0.0, 10.0),
    (10.0, 0.0, 10.0),
    (10.0, 10.0, 10.0),
    (0.0, 10.0, 10.0),
]
_CUBE_QUADS = [
    (0, 3, 2, 1),
    (4, 5, 6, 7),
    (0, 1, 5, 4),
    (1, 2, 6, 5),
    (2, 3, 7, 6),
    (3, 0, 4, 7),
]


def _cube_triangles() -> list[tuple[tuple[float, float, float], ...]]:
    triangles = []
    for a, b, c, d in _CUBE_QUADS:
        triangles.append((_CUBE_VERTICES[a], _CUBE_VERTICES[b], _CUBE_VERTICES[c]))
        triangles.append((_CUBE_VERTICES[a], _CUBE_VERTICES[c], _CUBE_VERTICES[d]))
    return triangles


def _write_ascii_stl(path: Path, *, degenerate: int = 0) -> None:
    lines = ["solid cube"]
    triangles = _cube_triangles() + [((0.0, 0.0, 0.0),) * 3] * degenerate
    for triangle in triangles:
        lines.append("  facet normal 0 0 0")
        lines.append("    outer loop")
        for vertex in triangle:
            lines.append("      vertex " + " ".join(f"{value:.1f}" for value in vertex))
        lines.append("    endloop")
        lines.append("  endfacet")
    lines.append("endsolid cube")
    path.write_text("\n".join(lines) + "\n", encoding="ascii")


def _write_binary_stl(path: Path, *, truncate: bool = False) -> None:
    triangles = _cube_triangles()
    payload = bytearray(b"cad3d-ir binary cube".ljust(80, b"\0"))
    payload += struct.pack("<I", len(triangles))
    for triangle in triangles:
        payload += struct.pack("<3f", 0.0, 0.0, 0.0)
        for vertex in triangle:
            payload += struct.pack("<3f", *vertex)
        payload += struct.pack("<H", 0)
    if truncate:
        payload = payload[:-50]
    path.write_bytes(bytes(payload))


def _write_obj(path: Path) -> None:
    lines = ["# cube", "mtllib cube.mtl", "o cube"]
    lines += ["v " + " ".join(f"{value:.1f}" for value in v) for v in _CUBE_VERTICES]
    lines.append("usemtl steel")
    for quad in _CUBE_QUADS[:-1]:
        lines.append("f " + " ".join(f"{index + 1}//1" for index in quad))
    # Last face uses negative (relative) indices.
    last = _CUBE_QUADS[-1]
    lines.append("f " + " ".join(str(index - len(_CUBE_VERTICES)) for index in last))
    lines.append("l 1 2 3")
    path.write_text("\n".join(lines) + "\n", encoding="ascii")


def test_mesh_importer_implements_core_protocol() -> None:
    assert isinstance(MeshImporter(), Importer)


def test_detects_stl_and_obj_formats(tmp_path: Path) -> None:
    ascii_path = tmp_path / "cube.stl"
    binary_path = tmp_path / "cube.bin"
    obj_path = tmp_path / "cube.obj"
    other = tmp_path / "notes.txt"
    _write_ascii_stl(ascii_path)
    _write_binary_stl(binary_path)
    _write_obj(obj_path)
    other.write_text("hello\n", encoding="ascii")

    assert detect_mesh_format(ascii_path) == "stl-ascii"
    assert detect_mesh_format(binary_path) == "stl-binary"
    assert detect_mesh_format(obj_path) == "obj"
    assert detect_mesh_format(other) is None
    assert detect_mesh_format(tmp_path / "missing.stl") is None
    assert MeshImporter().probe(ascii_path) == 1.0
    assert MeshImporter().probe(binary_path) == pytest.approx(0.7)
    assert MeshImporter().probe(other) == 0.0

    assert read_stl(ascii_path).triangle_count == 12
    assert read_stl(binary_path).triangle_count == 12
    obj = read_obj(obj_path)
    assert obj.triangle_count == 12
    assert obj.polygons_triangulated == 6
    assert obj.materials_present and obj.groups_present
    assert len(obj.polylines) == 1


def test_stl_import_writes_a_valid_package_and_glb(tmp_path: Path) -> None:
    source = tmp_path / "cube.stl"
    _write_ascii_stl(source, degenerate=2)
    output = tmp_path / "out"
    result = MeshImporter().import_file(
        source,
        options=ImportOptions(),
        resources=DirectoryResourceWriter(output),
    )
    package = result.package
    validate_package(package, resource_root=output)

    assert package.units.length_unit == "unknown"
    assert [product.kind for product in package.product_definitions] == ["part"]
    assert package.bodies[0].representation_ids == [package.representations[0].id]
    representation = package.representations[0]
    assert (representation.kind, representation.role) == ("mesh", "design")
    assert representation.bounds is not None
    assert representation.bounds.min == (0.0, 0.0, 0.0)
    assert representation.bounds.max == (10.0, 10.0, 10.0)
    assert package.visualizations[0].resource_id == representation.resource_id
    assert package.visualizations[0].statistics == {
        "triangles": 12,
        "line_segments": 0,
        "vertices": 36,
    }
    assert package.resources[0].media_type == "model/gltf-binary"
    assert package.resources[0].uri.endswith(".glb")
    report = validate_glb_bytes((output / package.resources[0].uri).read_bytes())
    assert report.valid, report.errors
    assert report.triangle_count == 12
    assert [item.code for item in result.diagnostics] == [
        "MESH_UNITS_UNKNOWN",
        "MESH_DEGENERATE_TRIANGLES_DROPPED",
    ]
    assert result.diagnostics[1].details == {"count": 2}
    assert result.statistics["triangles"] == 12
    assert result.statistics["source_format"] == "stl-ascii"


def test_binary_stl_length_mismatch_is_reported(tmp_path: Path) -> None:
    source = tmp_path / "cube.stl"
    _write_binary_stl(source, truncate=True)
    result = MeshImporter().import_file(
        source,
        options=ImportOptions(),
        resources=DirectoryResourceWriter(tmp_path / "out"),
    )
    assert result.statistics["triangles"] == 11
    assert "STL_BINARY_LENGTH_MISMATCH" in [item.code for item in result.diagnostics]


def test_obj_import_keeps_lines_and_reports_simplifications(tmp_path: Path) -> None:
    source = tmp_path / "cube.obj"
    _write_obj(source)
    output = tmp_path / "out"
    result = MeshImporter().import_file(
        source,
        options=ImportOptions(),
        resources=DirectoryResourceWriter(output),
    )
    validate_package(result.package, resource_root=output)
    codes = [item.code for item in result.diagnostics]
    assert codes == [
        "MESH_UNITS_UNKNOWN",
        "OBJ_POLYGONS_TRIANGULATED",
        "OBJ_MATERIALS_IGNORED",
        "OBJ_GROUPS_FLATTENED",
    ]
    assert result.statistics["triangles"] == 12
    assert result.statistics["line_segments"] == 2
    report = validate_glb_bytes((output / result.package.resources[0].uri).read_bytes())
    assert report.valid, report.errors
    assert (report.triangle_count, report.line_segment_count) == (12, 2)


def test_mesh_import_rejects_empty_or_foreign_sources(tmp_path: Path) -> None:
    empty = tmp_path / "empty.stl"
    empty.write_text("solid empty\nendsolid empty\n", encoding="ascii")
    with pytest.raises(MeshImportError, match="no geometry"):
        MeshImporter().import_file(
            empty,
            options=ImportOptions(),
            resources=DirectoryResourceWriter(tmp_path / "out"),
        )
    text = tmp_path / "notes.txt"
    text.write_text("hello\n", encoding="ascii")
    with pytest.raises(MeshImportError, match="not an STL or OBJ"):
        MeshImporter().import_file(
            text,
            options=ImportOptions(),
            resources=DirectoryResourceWriter(tmp_path / "out2"),
        )
    with pytest.raises(MeshImportError, match="supports IR"):
        MeshImporter().import_file(
            empty,
            options=ImportOptions(ir_version="9.9.9"),
            resources=DirectoryResourceWriter(tmp_path / "out3"),
        )


def test_import_cli_writes_package(tmp_path: Path, capsys) -> None:
    source = tmp_path / "cube.obj"
    _write_obj(source)
    output = tmp_path / "package"
    diagnostics = tmp_path / "diagnostics.json"
    assert (
        main(["import", str(source), str(output), "--diagnostics", str(diagnostics)])
        == 0
    )
    package = load_manifest(output / "manifest.json")
    validate_package(package, resource_root=output)
    report = json.loads(diagnostics.read_text(encoding="utf-8"))
    assert report["statistics"]["triangles"] == 12
    assert "12 triangles, 2 line segments, 0 warnings" in capsys.readouterr().out
    assert main(["import", str(source), str(output)]) == 2
    assert main(["import", str(tmp_path / "missing.obj"), str(tmp_path / "p2")]) == 1
