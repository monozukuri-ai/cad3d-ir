from __future__ import annotations

import json
import math
from hashlib import sha256
from pathlib import Path

import pytest
from cad3d_ir import Resource
from cad3d_ir.importers import ImportOptions
from cad3d_ir.mesh import GlbBuilder, read_glb, vertex_normals

from cad3d_ir_step import (
    IgesImporter,
    MeshBody,
    MeshTooLargeError,
    StepExportError,
    StepImporter,
    export_step,
    mesh_to_step,
    write_mesh_step,
)
from cad3d_ir_step.export import bodies_from_scene, source_format_of
from cad3d_ir_step.export_cli import main as export_main
from cad3d_ir_step.header import inspect_step_header

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


class _Memory:
    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}

    def write_chunks(
        self,
        *,
        id,
        uri,
        chunks,
        media_type,
        encoding,
        name=None,
        source=None,
        properties=None,
        extensions=None,
    ):  # noqa: E501
        data = b"".join(chunks)
        self.blobs[id] = data
        return Resource(
            id=id,
            name=name,
            uri=uri,
            media_type=media_type,
            encoding=encoding,
            sha256=sha256(data).hexdigest(),
            byte_length=len(data),
            source=source,
            properties=properties or {},
            extensions=extensions or {},
        )


def _reimport(path: Path):
    """Products, occurrences, distinct colours, placed triangles, declared schemas."""
    importer: StepImporter | IgesImporter = (
        IgesImporter(body_mesh_resources=False)
        if source_format_of(path) == "iges"
        else StepImporter(body_mesh_resources=False)
    )
    result = importer.import_file(
        path,
        options=ImportOptions(
            strict=False, include_visualization_mesh=True, include_brep=False
        ),
        resources=_Memory(),
    )
    names = sorted(product.name for product in result.package.product_definitions)
    colors = sorted(
        {
            tuple(round(c, 2) for c in appearance.base_color)
            for appearance in result.package.appearances
            if appearance.base_color
        }
    )
    triangles = result.package.visualizations[0].statistics["triangles"]
    return names, len(result.package.occurrences), colors, triangles, result


def _cubes_glb(
    *, offsets=(0.0, 20.0), degenerate: bool = False, lines: bool = True
) -> bytes:
    builder = GlbBuilder(root_name="cubes")
    materials = [builder.add_material("red", RED), builder.add_material("blue", BLUE)]
    for index, offset in enumerate(offsets):
        mesh = builder.add_mesh(f"cube-{index}")
        indices = list(CUBE_INDICES) + ([0, 0, 1] if degenerate else [])
        vertex_set = builder.add_vertices(
            positions=CUBE_POSITIONS,
            normals=vertex_normals(CUBE_POSITIONS, CUBE_INDICES),
        )
        builder.add_triangles(
            mesh, indices=indices, vertices=vertex_set, material=materials[index % 2]
        )
        if lines:
            builder.add_lines(mesh, indices=[0, 1, 1, 2], vertices=vertex_set)
        matrix = (
            (1.0, 0.0, 0.0, offset),
            (0.0, 1.0, 0.0, 0.0),
            (0.0, 0.0, 1.0, 0.0),
            (0.0, 0.0, 0.0, 1.0),
        )
        builder.add_node(f"cube-{'ab'[index % 2]}", mesh=mesh, matrix=matrix)
    return builder.build()


@pytest.mark.parametrize(
    ("schema", "declared"),
    [
        ("AP203", "CONFIG_CONTROL_DESIGN"),
        ("AP214", "AUTOMOTIVE_DESIGN"),
        ("AP242", "AP242_MANAGED_MODEL_BASED_3D_ENGINEERING"),
    ],
)
def test_export_step_keeps_structure_colours_and_sets_the_schema(
    fixtures_dir: Path, tmp_path: Path, schema: str, declared: str, capfd
) -> None:
    source = fixtures_dir / "reference-assembly.step"
    original = _reimport(source)
    target = tmp_path / f"reference-{schema}.step"

    result = export_step(source, target, schema=schema)

    assert result.path == target and result.byte_length == target.stat().st_size
    assert result.schema == schema and result.unit == "mm"
    assert result.source_format == "step"
    assert (result.simple_shapes, result.assemblies, result.components) == (2, 2, 4)
    assert result.products == 4
    assert result.colored_shapes == 2 and result.colors_copied == 3
    assert result.colored_shapes_after is None
    assert result.source_length_units == ("millimetre",)
    assert [item.code for item in result.diagnostics] == [
        "STEP_EXPORT_WRITTEN",
        "STEP_COLORS_COPIED",
    ]
    assert any(
        item.startswith(declared) for item in inspect_step_header(target).schemas
    )
    names, occurrences, colors, triangles, _ = _reimport(target)
    assert (names, occurrences, colors, triangles) == original[:4]
    assert "Statistics on Transfer" not in capfd.readouterr().out


def test_export_step_verify_and_colour_options(
    fixtures_dir: Path, tmp_path: Path
) -> None:
    source = fixtures_dir / "reference-assembly.step"

    verified = export_step(source, tmp_path / "verified.step", verify=True)
    plain = export_step(source, tmp_path / "plain.step", keep_colors=False)

    assert verified.colored_shapes_after is not None
    assert verified.colored_shapes_after >= verified.colored_shapes
    assert "STEP_COLORS_CHANGED" not in {item.code for item in verified.diagnostics}
    assert plain.colors_copied == 0
    assert [item.code for item in plain.diagnostics] == ["STEP_EXPORT_WRITTEN"]


def test_export_step_from_iges_in_inches(fixtures_dir: Path, tmp_path: Path) -> None:
    source = fixtures_dir / "reference-assembly.iges"
    original = _reimport(source)
    target = tmp_path / "from-iges.step"

    result = export_step(source, target, unit="inch")

    assert result.source_format == "iges" and source_format_of(source) == "iges"
    assert result.unit == "inch"
    names, occurrences, colors, triangles, reimported = _reimport(target)
    assert triangles == original[3]
    assert [unit.lower() for unit in reimported.statistics["source_length_units"]] == [
        "inch"
    ]
    bounds = reimported.package.visualizations[0].bounds
    assert bounds is not None
    assert tuple(round(v, 3) for v in bounds.max) == (40.0, 20.0, 12.0)


def test_tessellated_step_sources_are_read_and_rewritten_only_as_ap242(
    tmp_path: Path,
) -> None:
    source = tmp_path / "tessellated.step"
    written = write_mesh_step(
        read_glb(_cubes_glb(lines=False)),
        source,
        schema="AP242",
        representation="tessellated",
    )
    assert (written.parts, written.tessellated_faces) == (2, 2)
    # the importer reads the triangles and tells that no face has a surface
    names, occurrences, _, triangles, result = _reimport(source)
    assert (names, occurrences, triangles) == (["assembly", "cube-0", "cube-1"], 2, 24)
    statistics = result.package.visualizations[0].statistics
    assert (statistics["faces"], statistics["tessellated_faces"]) == (2, 2)
    tessellated = next(
        d for d in result.diagnostics if d.code == "STEP_TESSELLATED_FACES"
    )
    assert tessellated.details == {"faces": 2, "all_faces": 2}
    # AP214 has no tessellated entities: the faces are left out, not written
    # as AP242 entities into an AP214 file
    ap214 = export_step(source, tmp_path / "ap214.step", schema="AP214")
    assert (ap214.faces, ap214.tessellated_faces) == (2, 2)
    text = (tmp_path / "ap214.step").read_text(encoding="utf-8", errors="replace")
    assert "TRIANGULATED_FACE" not in text and "AUTOMOTIVE_DESIGN" in text
    dropped = next(
        d for d in ap214.diagnostics if d.code == "STEP_TESSELLATED_NOT_WRITTEN"
    )
    assert dropped.severity == "warning"
    assert dropped.details == {"faces": 2, "all_faces": 2, "schema": "AP214"}
    assert _reimport(tmp_path / "ap214.step")[3] == 0
    # AP242 keeps them
    ap242 = export_step(source, tmp_path / "ap242.step", schema="AP242")
    assert {d.code for d in ap242.diagnostics} >= {"STEP_TESSELLATED_WRITTEN"}
    text = (tmp_path / "ap242.step").read_text(encoding="utf-8", errors="replace")
    assert "TRIANGULATED_FACE" in text
    assert _reimport(tmp_path / "ap242.step")[3] == 24


def test_export_step_rejects_bad_sources_and_options(
    tmp_path: Path, fixtures_dir: Path
) -> None:
    text = tmp_path / "notes.txt"
    text.write_text("hello", encoding="utf-8")
    with pytest.raises(StepExportError, match="not a STEP"):
        export_step(text, tmp_path / "out.step")
    with pytest.raises(StepExportError, match="not a file"):
        export_step(tmp_path / "missing.step", tmp_path / "out.step")
    source = fixtures_dir / "reference-assembly.step"
    with pytest.raises(ValueError, match="schema"):
        export_step(source, tmp_path / "out.step", schema="AP2000")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unit"):
        export_step(source, tmp_path / "out.step", unit="furlong")  # type: ignore[arg-type]


def test_mesh_to_step_builds_named_coloured_solids(tmp_path: Path, capfd) -> None:
    glb = _cubes_glb()
    target = tmp_path / "cubes.step"

    result = mesh_to_step(glb, target, name="two cubes")

    assert result.path == target and result.byte_length == target.stat().st_size
    assert result.schema == "AP214" and result.mode == "solid"
    assert (result.bodies, result.triangles, result.vertices) == (2, 24, 16)
    assert (result.solids, result.open_shells, result.free_edges) == (2, 0, 0)
    assert result.degenerate_triangles == 0
    assert result.volume_mm3 is not None and math.isclose(result.volume_mm3, 2000.0)
    assert result.names == ("cube-a", "cube-b")
    assert [item.code for item in result.diagnostics] == ["STEP_MESH_SOLID_FACETED"]
    names, occurrences, colors, triangles, _ = _reimport(target)
    assert names == ["cube-a", "cube-b", "two cubes"]
    assert occurrences == 2
    assert colors == sorted(
        [tuple(round(c, 2) for c in BLUE), tuple(round(c, 2) for c in RED)]
    )
    assert triangles == 24
    assert "Statistics on Transfer" not in capfd.readouterr().out


def test_mesh_to_step_single_body_drops_degenerate_triangles(tmp_path: Path) -> None:
    glb = _cubes_glb(offsets=(0.0,), degenerate=True)
    scene = read_glb(glb)
    bodies, merged = bodies_from_scene(scene)
    assert [body.name for body in bodies] == ["cube-a"] and merged == 0
    assert bodies[0].color == RED

    result = mesh_to_step(scene, tmp_path / "cube.step")

    assert result.bodies == 1 and result.triangles == 12
    assert result.degenerate_triangles == 1
    assert {item.code for item in result.diagnostics} == {
        "STEP_MESH_SOLID_FACETED",
        "STEP_MESH_DEGENERATE_DROPPED",
    }
    names, occurrences, colors, triangles, _ = _reimport(result.path)
    assert names == ["cube-a"] and occurrences == 0
    assert colors == [tuple(round(c, 2) for c in RED)] and triangles == 12


def test_mesh_to_step_reports_open_shells_and_enforces_the_cap(tmp_path: Path) -> None:
    triangle = MeshBody(
        name="flap",
        positions=[0.0, 0.0, 0.0, 10.0, 0.0, 0.0, 0.0, 10.0, 0.0],
        indices=[0, 1, 2],
    )

    result = mesh_to_step([triangle], tmp_path / "flap.step")

    assert (result.solids, result.open_shells, result.free_edges) == (0, 1, 3)
    assert result.volume_mm3 == 0.0
    assert "STEP_MESH_FREE_EDGES" in {item.code for item in result.diagnostics}
    assert result.names == ("flap",)

    with pytest.raises(MeshTooLargeError) as info:
        mesh_to_step(
            _cubes_glb(offsets=(0.0,)), tmp_path / "capped.step", max_triangles=10
        )
    assert (info.value.triangles, info.value.limit) == (12, 10)
    assert "12 triangles" in str(info.value)

    empty = MeshBody(name="nothing", positions=[0.0, 0.0, 0.0] * 3, indices=[0, 1, 2])
    with pytest.raises(StepExportError, match="no triangles"):
        mesh_to_step([empty], tmp_path / "empty.step")
    with pytest.raises(TypeError):
        mesh_to_step(["not a body"], tmp_path / "bad.step")  # type: ignore[list-item]
    with pytest.raises(StepExportError, match="cannot read the GLB"):
        mesh_to_step(b"glTFjunk" * 4, tmp_path / "junk.step")


def test_mesh_to_step_tessellated_forces_ap242(tmp_path: Path) -> None:
    target = tmp_path / "tess.step"

    result = mesh_to_step(
        _cubes_glb(offsets=(0.0,)), target, mode="tessellated", schema="AP214"
    )

    assert result.schema == "AP242" and result.mode == "tessellated"
    assert result.volume_mm3 is None and result.solids == 0
    assert [item.code for item in result.diagnostics] == ["STEP_MESH_TESSELLATED"]
    text = target.read_text(encoding="utf-8", errors="replace")
    assert "TRIANGULATED_FACE" in text and "TESSELLATED_SHELL" in text
    assert "AP242_MANAGED_MODEL_BASED_3D_ENGINEERING" in text
    assert result.byte_length < 8_000


def test_mesh_to_step_scales_other_package_units_to_millimetres(tmp_path: Path) -> None:
    builder = GlbBuilder(root_name="metres", length_unit_to_m=1.0)
    mesh = builder.add_mesh("cube")
    positions = [value / 1000.0 for value in CUBE_POSITIONS]  # a 10 mm cube in metres
    builder.add_triangles(mesh, positions=positions, indices=CUBE_INDICES)
    builder.add_node("cube", mesh=mesh)

    result = mesh_to_step(builder.build(), tmp_path / "metres.step")

    assert result.volume_mm3 is not None and math.isclose(
        result.volume_mm3, 1000.0, rel_tol=1e-6
    )


def test_export_cli_writes_step_from_step_and_glb(
    fixtures_dir: Path, tmp_path: Path, capsys
) -> None:
    source = fixtures_dir / "reference-assembly.step"
    out = tmp_path / "cli.step"
    report = tmp_path / "report.json"

    assert (
        export_main(
            [str(source), str(out), "--schema", "AP203", "--diagnostics", str(report)]
        )
        == 0
    )
    captured = capsys.readouterr().out
    assert "4 products, 4 components, 2 coloured shapes (3 colours copied)" in captured
    assert "(AP203, mm), 0 warnings" in captured
    data = json.loads(report.read_text(encoding="utf-8"))
    assert data["statistics"]["schema"] == "AP203"
    assert [item["code"] for item in data["diagnostics"]] == [
        "STEP_EXPORT_WRITTEN",
        "STEP_COLORS_COPIED",
    ]

    glb = tmp_path / "cubes.glb"
    glb.write_bytes(_cubes_glb())
    mesh_out = tmp_path / "cubes.step"
    assert export_main([str(glb), str(mesh_out), "--name", "cubes"]) == 0
    assert "2 bodies, 24 triangles, 2 solids, 0 open shells" in capsys.readouterr().out
    assert _reimport(mesh_out)[0] == ["cube-a", "cube-b", "cubes"]

    assert export_main([str(glb), str(mesh_out), "--max-triangles", "10"]) == 3
    assert "accepts at most 10" in capsys.readouterr().err

    bad = tmp_path / "bad.step"
    bad.write_text("not step", encoding="utf-8")
    assert export_main([str(bad), str(out)]) == 1
    assert "error:" in capsys.readouterr().err
