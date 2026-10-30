from __future__ import annotations

import struct

import pytest

from cad3d_ir_edrawings.display import (
    IDENTITY,
    color_key,
    color_name,
    instance_matrix,
    local_bounds,
    placed_bounds,
    scale_positions,
    transform_point,
)

# 90 degrees about Z then a translation of (10, 20, 30) model units, as the kit
# stores it: row-major for row vectors, translation in the last row.
ROTATED = (0, 1, 0, 0, -1, 0, 0, 0, 0, 0, 1, 0, 10, 20, 30, 1)


def test_kit_matrices_become_column_vector_matrices_in_millimetres() -> None:
    matrix = instance_matrix(ROTATED, 1000.0)
    assert matrix[0] == (0.0, -1.0, 0.0, 10000.0)
    assert matrix[1] == (1.0, 0.0, 0.0, 20000.0)
    assert matrix[2] == (0.0, 0.0, 1.0, 30000.0)
    assert matrix[3] == (0.0, 0.0, 0.0, 1.0)
    # The row-vector product p·M and the column-vector product M·p agree.
    p = (1.0, 2.0, 3.0)
    row_vector = (
        p[0] * ROTATED[0] + p[1] * ROTATED[4] + p[2] * ROTATED[8] + ROTATED[12],
        p[0] * ROTATED[1] + p[1] * ROTATED[5] + p[2] * ROTATED[9] + ROTATED[13],
        p[0] * ROTATED[2] + p[1] * ROTATED[6] + p[2] * ROTATED[10] + ROTATED[14],
    )
    column = transform_point(instance_matrix(ROTATED, 1.0), p)
    assert column == pytest.approx(row_vector)
    assert instance_matrix([1.0] + [0.0] * 14 + [1.0], 25.4)[0] == (1.0, 0.0, 0.0, 0.0)
    with pytest.raises(ValueError, match="sixteen"):
        instance_matrix([1.0] * 12, 1.0)
    with pytest.raises(ValueError, match="finite"):
        instance_matrix([float("nan")] + [0.0] * 15, 1.0)


def test_positions_are_scaled_in_place_and_bounded() -> None:
    payload = struct.pack("<9f", 0, 0, 0, 1, 2, 3, -1, 0.5, 4)
    assert scale_positions(payload, 1.0) == payload
    scaled = scale_positions(payload, 1000.0)
    assert struct.unpack("<9f", scaled) == pytest.approx(
        (0, 0, 0, 1000, 2000, 3000, -1000, 500, 4000)
    )
    assert local_bounds(scaled) == ((-1000.0, 0.0, 0.0), (1000.0, 2000.0, 4000.0))
    assert local_bounds(b"") is None
    bounds = placed_bounds(None, ((0.0, 0.0, 0.0), (2.0, 3.0, 4.0)), IDENTITY)
    assert bounds == ((0.0, 0.0, 0.0), (2.0, 3.0, 4.0))
    moved = placed_bounds(
        bounds, ((0.0, 0.0, 0.0), (2.0, 3.0, 4.0)), instance_matrix(ROTATED, 1.0)
    )
    # The rotated box spans x in [10 - 3, 10], y in [20, 22], z in [30, 34].
    assert moved[0] == pytest.approx((0.0, 0.0, 0.0))
    assert moved[1] == pytest.approx((10.0, 22.0, 34.0))


def test_colours_follow_the_kit_alpha_convention() -> None:
    assert color_key((255, 0, 0, 255)) == (1.0, 0.0, 0.0, 1.0)
    assert color_key((10, 20, 30, 0)) is None
    assert color_name(None) == "faces"
    assert color_name((1.0, 0.0, 0.0, 1.0)) == "#ff0000"
    assert color_name((0.0, 0.0, 1.0, 0.5)) == "#0000ff/80"
