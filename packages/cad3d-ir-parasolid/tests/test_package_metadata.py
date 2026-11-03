from __future__ import annotations

from importlib.metadata import metadata, version
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

import cad3d_ir_parasolid

ADAPTER_ROOT = Path(__file__).parents[1]
REPOSITORY_ROOT = ADAPTER_ROOT.parents[1]


def test_adapter_depends_on_core_step_and_parasolid_kit_only_in_this_direction() -> (
    None
):
    adapter = tomllib.loads(
        (ADAPTER_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    core = tomllib.loads(
        (REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    dependencies = adapter["project"]["dependencies"]
    assert any(item.startswith("cad3d-ir>=") for item in dependencies)
    assert any(item.startswith("cad3d-ir-step>=") for item in dependencies)
    assert any(item.startswith("parasolid-kit[occt]>=") for item in dependencies)
    assert all("parasolid" not in item for item in core["project"]["dependencies"])
    assert adapter["project"]["license"] == "MIT"
    assert adapter["project"]["version"] == cad3d_ir_parasolid.__version__
    assert "packages/cad3d-ir-parasolid" in core["tool"]["uv"]["workspace"]["members"]


def test_distribution_metadata_carries_the_mit_license() -> None:
    distribution = metadata("cad3d-ir-parasolid")
    assert version("cad3d-ir-parasolid") == cad3d_ir_parasolid.__version__
    assert distribution["License-Expression"] == "MIT"
    assert "LICENSE" in (distribution.get_all("License-File") or [])
