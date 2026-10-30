from __future__ import annotations

from importlib.metadata import metadata, version
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

import cad3d_ir_edrawings

ADAPTER_ROOT = Path(__file__).parents[1]
REPOSITORY_ROOT = ADAPTER_ROOT.parents[1]


def test_adapter_depends_on_core_and_edrawings_kit_only_in_this_direction() -> None:
    adapter = tomllib.loads(
        (ADAPTER_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    core = tomllib.loads(
        (REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    dependencies = adapter["project"]["dependencies"]
    assert any(item.startswith("cad3d-ir>=") for item in dependencies)
    assert any(item.startswith("edrawings-kit>=") for item in dependencies)
    assert all("edrawings" not in item for item in core["project"]["dependencies"])
    assert adapter["project"]["license"] == "MIT"
    assert adapter["project"]["version"] == cad3d_ir_edrawings.__version__


def test_distribution_metadata_carries_the_mit_license() -> None:
    distribution = metadata("cad3d-ir-edrawings")
    assert version("cad3d-ir-edrawings") == cad3d_ir_edrawings.__version__
    assert distribution["License-Expression"] == "MIT"
    license_files = distribution.get_all("License-File") or []
    assert "LICENSE" in license_files
    text = (ADAPTER_ROOT / "LICENSE").read_text(encoding="utf-8")
    assert text.startswith("MIT License")
