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
    """Root plus one child part whose saved final body is an authored box."""
    return _copy(tmp_path, "box-v7l7.icd")


@pytest.fixture
def external_path(tmp_path: Path) -> Path:
    """Root plus one child that references another file (no geometry here)."""
    return _copy(tmp_path, "external-v7l7.icd")


@pytest.fixture
def sheet_path(tmp_path: Path) -> Path:
    """A 2D document (global view plus one 2D view) without parts."""
    return _copy(tmp_path, "sheet-v8l3.icd")


@pytest.fixture
def v8l4_path(tmp_path: Path, box_path: Path) -> Path:
    """The box document with its version bytes set to an unqualified V8L4."""
    data = bytearray(box_path.read_bytes())
    data[12:16] = b"\0\x08\0\x04"
    path = tmp_path / "box-v8l4.icd"
    path.write_bytes(bytes(data))
    return path
