from __future__ import annotations

from importlib.metadata import metadata, version
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

import cad3d_ir_inventor

ADAPTER_ROOT = Path(__file__).parents[1]
REPOSITORY_ROOT = ADAPTER_ROOT.parents[1]


def test_adapter_depends_on_core_step_and_inventor_kit_only_in_this_direction() -> None:
    adapter = tomllib.loads(
        (ADAPTER_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    core = tomllib.loads(
        (REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    dependencies = adapter["project"]["dependencies"]
    assert any(item.startswith("cad3d-ir>=") for item in dependencies)
    assert any(item.startswith("cad3d-ir-step>=0.4.3") for item in dependencies)
    # The OCCT extra only: the adapter must not pull CadQuery in.
    assert any(item.startswith("inventor-kit[occt]>=0.10") for item in dependencies)
    assert all("cadquery]" not in item for item in dependencies)
    assert all("inventor" not in item for item in core["project"]["dependencies"])
    assert adapter["project"]["license"] == "PolyForm-Noncommercial-1.0.0"
    assert adapter["project"]["version"] == cad3d_ir_inventor.__version__
    assert "packages/cad3d-ir-inventor" in core["tool"]["uv"]["workspace"]["members"]


def test_distribution_metadata_carries_the_polyform_license() -> None:
    distribution = metadata("cad3d-ir-inventor")
    assert version("cad3d-ir-inventor") == cad3d_ir_inventor.__version__
    assert distribution["License-Expression"] == "PolyForm-Noncommercial-1.0.0"
    license_files = distribution.get_all("License-File") or []
    assert "LICENSE" in license_files
    assert any(
        name.endswith("PolyForm-Noncommercial-1.0.0.md") for name in license_files
    )
    notice = (ADAPTER_ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "Required Notice: cad3d-ir-inventor is licensed by UnRobotics Inc." in notice
