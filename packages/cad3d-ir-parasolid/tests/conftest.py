from __future__ import annotations

import shutil
from pathlib import Path

import pytest

DATA = Path(__file__).parent / "data"


def _copy(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    shutil.copyfile(DATA / name, path)
    return path


@pytest.fixture
def box_path(tmp_path: Path) -> Path:
    """An Onshape box exported as X_T (one solid, a body colour attribute)."""
    return _copy(tmp_path, "onshape-box-v30.x_t")


@pytest.fixture
def box_binary_path(tmp_path: Path) -> Path:
    """The same box exported as X_B."""
    return _copy(tmp_path, "onshape-box-v30.x_b")


@pytest.fixture
def cylinder_path(tmp_path: Path) -> Path:
    """An Onshape cylinder with a hole: a named body with face colours."""
    return _copy(tmp_path, "onshape-cylinder-hole-v30.x_t")


@pytest.fixture
def assembly_path(tmp_path: Path) -> Path:
    """The reference's disc (a coloured sheet) placed twice by two assemblies.

    Root assembly 21 holds instance 22 of the disc (a quarter turn about z and
    a translation of (1, 2, 3) m) and the negative instance 24 of sub-assembly
    25, which holds instance 26 of the disc without a transform.
    """
    return _copy(tmp_path, "disc-assembly-13006.x_t")


@pytest.fixture
def unsupported_path(tmp_path: Path, box_path: Path) -> Path:
    """The box with its stream key moved to a schema no profile covers."""
    data = box_path.read_bytes().replace(b"SCH_3000000_30000", b"SCH_3000000_30900")
    path = tmp_path / "box-30900.x_t"
    path.write_bytes(data)
    return path
