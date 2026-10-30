from __future__ import annotations

from pathlib import Path

import pytest

from tests.synthetic import model_hsf, zip_container

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def part_path(tmp_path: Path) -> Path:
    """A raw HSF part (plate + two shared pins) in millimetres."""
    path = tmp_path / "Bracket-01.eprt"
    path.write_bytes(model_hsf())
    return path


@pytest.fixture
def assembly_path(tmp_path: Path) -> Path:
    """The same model inside a ZIP container with a preview, as an assembly."""
    path = tmp_path / "Frame.easm"
    path.write_bytes(zip_container(model_hsf()))
    return path


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES
