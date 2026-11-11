from __future__ import annotations

import shutil
from importlib.util import find_spec
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cad3d_ir_inventor import InventorSource

DATA = Path(__file__).parent / "data"

needs_kit = pytest.mark.skipif(
    find_spec("inventor_kit") is None,
    reason="inventor-kit is not installed (it needs Python 3.11 or later)",
)

# A 1 x 1 PNG.
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000b49444154789c6360000200000500017a5eab3f0000000049454e44ae426082"
)


def _copy(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    shutil.copyfile(DATA / name, path)
    return path


@pytest.fixture
def box_path(tmp_path: Path) -> Path:
    """A 10 x 20 x 30 mm box written by inventor-kit (segment major 31)."""
    return _copy(tmp_path, "box-10-20-30.ipt")


@pytest.fixture
def ring_path(tmp_path: Path) -> Path:
    """A cylinder with a through hole written for Inventor 2026 (major 30)."""
    return _copy(tmp_path, "ring-r10-r4-h20.ipt")


def box_shape(dx: float = 10.0, dy: float = 20.0, dz: float = 30.0) -> Any:
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

    return BRepPrimAPI_MakeBox(dx, dy, dz).Shape()


def converted_body(index: int, *, approximations: tuple[dict, ...] = ()) -> Any:
    """A stand-in for a converted ``inventor_kit.BodyConversion``."""
    warnings = (
        (
            SimpleNamespace(
                code="geometry.approximated_from_stored_spline", message="fits"
            ),
        )
        if approximations
        else ()
    )
    return SimpleNamespace(
        body_index=index,
        id=f"body-{index:064d}",
        status="converted_solid",
        shape=box_shape(),
        metrics={
            "valid": True,
            "solids": 1,
            "faces": 6,
            "volume_mm3": 6000.0,
            "area_mm2": 2200.0,
            "bbox_mm": [0.0, 0.0, 0.0, 10.0, 20.0, 30.0],
        },
        diagnostics=(),
        warnings=warnings,
        approximations=approximations,
    )


def rejected_body(
    index: int, status: str, code: str, message: str, subtype: str | None = None
) -> Any:
    """A stand-in for a body inventor-kit did not convert."""
    return SimpleNamespace(
        body_index=index,
        id=f"body-{index:064d}",
        status=status,
        shape=None,
        metrics=None,
        diagnostics=(SimpleNamespace(code=code, message=message, subtype=subtype),),
        warnings=(),
        approximations=(),
    )


def stand_in_source(
    tmp_path: Path,
    *,
    kind: str = "part",
    bodies: tuple[Any, ...] | None = (),
    major: int | None = 25,
    profile_status: str = "supported_stored_table",
    candidate_codes: tuple[str, ...] = (),
    reasons: tuple[str, ...] = (),
    thumbnails: tuple[Any, ...] = (),
    kernel_missing: bool = False,
) -> InventorSource:
    """An :class:`InventorSource` over stand-in inventor-kit objects.

    ``bodies=None`` is a document without a selected stored table.
    """
    path = tmp_path / "stand-in.ipt"
    path.write_bytes(b"stand-in")
    candidate = SimpleNamespace(
        id="candidate",
        profile_status=profile_status,
        diagnostics=tuple(SimpleNamespace(code=code) for code in candidate_codes),
        carrier=SimpleNamespace(segment_major=major),
    )
    document = SimpleNamespace(
        metadata=SimpleNamespace(
            identification=SimpleNamespace(kind=kind),
            geometry_profile=SimpleNamespace(segment_major=major, sab_version=22600),
            property_sets=(),
            thumbnails=thumbnails,
        ),
        geometry=SimpleNamespace(
            candidates=(candidate,),
            selection=SimpleNamespace(
                selected_id=None if bodies is None else "candidate"
            ),
        ),
        model=None if bodies is None else object(),
        summary={"diagnostics": list(reasons)},
    )
    conversion = (
        None
        if bodies is None or kernel_missing
        else SimpleNamespace(bodies=bodies, current_state_verified=False)
    )
    return InventorSource(
        path=path,
        digest="0123456789abcdef" * 4,
        byte_length=8,
        document=document,
        read_error=None,
        conversion=conversion,
        conversion_error=None,
        kernel_missing=kernel_missing,
        read_seconds=0.0,
        convert_seconds=0.0,
    )
