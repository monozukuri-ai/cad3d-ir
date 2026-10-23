from __future__ import annotations

from pathlib import Path

import pytest

from tests.synthetic import synthetic_document


@pytest.fixture
def part_path(tmp_path: Path) -> Path:
    path = tmp_path / "Bracket-01.SLDPRT"
    path.write_bytes(synthetic_document())
    return path
