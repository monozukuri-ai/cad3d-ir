from __future__ import annotations

import pytest

from cad3d_ir_icad.shapes import (
    IDENTITY,
    GlbNode,
    build_glb,
    diagonal,
    merge_bounds,
    sharing_key,
    transform_bounds,
)


class _Body:
    def __init__(self, source: str, digest: str | None, body_id: str = "b") -> None:
        self.source = source
        self.payload_sha256 = digest
        self.body_id = body_id


def _box(dx: float, dy: float, dz: float):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

    return BRepPrimAPI_MakeBox(dx, dy, dz).Shape()


def test_sharing_key_depends_on_payload_bounds_and_parity() -> None:
    bounds = ((0.0, 0.0, 0.0), (1.0, 2.0, 3.0))
    same = sharing_key([_Body("saved", "a" * 64)], bounds, mirrored=False)
    assert same == sharing_key([_Body("saved", "a" * 64)], bounds, mirrored=False)
    assert same != sharing_key([_Body("saved", "a" * 64)], bounds, mirrored=True)
    moved = ((0.0, 0.0, 0.0), (1.0, 2.0, 3.5))
    assert same != sharing_key([_Body("saved", "a" * 64)], moved, mirrored=False)
    assert sharing_key(
        [_Body("native", None, "x")], bounds, mirrored=False
    ) != sharing_key([_Body("native", None, "y")], bounds, mirrored=False)


def test_bounds_helpers() -> None:
    bounds = ((0.0, 0.0, 0.0), (1.0, 2.0, 2.0))
    assert diagonal(bounds) == pytest.approx(3.0)
    assert diagonal(None) == 0.0
    rotated = transform_bounds(
        bounds,
        (
            (0.0, -1.0, 0.0, 10.0),
            (1.0, 0.0, 0.0, 0.0),
            (0.0, 0.0, 1.0, 0.0),
            (0, 0, 0, 1),
        ),
    )
    assert rotated == ((8.0, 0.0, 0.0), (10.0, 1.0, 2.0))
    assert merge_bounds(None, bounds) == bounds
    assert merge_bounds(bounds, rotated) == ((0.0, 0.0, 0.0), (10.0, 2.0, 2.0))


def test_build_glb_shares_meshes_between_nodes_and_places_them() -> None:
    nodes = [
        GlbNode("root", None, IDENTITY, None),
        GlbNode("a", 0, IDENTITY, "box"),
        GlbNode(
            "b",
            0,
            (
                (1.0, 0.0, 0.0, 100.0),
                (0.0, 1.0, 0.0, 0.0),
                (0.0, 0.0, 1.0, 0.0),
                (0, 0, 0, 1),
            ),
            "box",
        ),
        GlbNode("empty", 1, IDENTITY, None),
    ]
    result = build_glb(
        nodes,
        {"box": _box(10.0, 20.0, 30.0)},
        linear_deflection=0.1,
        angular_deflection=0.5,
        include_edges=True,
        generator="test",
        root_name="test",
    )
    assert result.mesh_count == 1 and result.node_count == 5
    # Primitive counts describe the one mesh; placed counts follow the two nodes.
    assert result.triangle_count == 12 and result.line_segment_count == 12
    assert result.placed_triangles == 24 and result.placed_line_segments == 24
    assert result.bounds == ((0.0, 0.0, 0.0), (10.0, 20.0, 30.0))
    assert result.placed_bounds == ((0.0, 0.0, 0.0), (110.0, 20.0, 30.0))
    assert result.failed_keys == {}
    assert result.mesh_triangles == {"box": 12}
