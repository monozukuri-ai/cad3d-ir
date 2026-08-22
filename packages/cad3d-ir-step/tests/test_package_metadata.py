from __future__ import annotations

from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


def test_step_adapter_and_core_keep_the_dependency_direction() -> None:
    adapter_root = Path(__file__).parents[1]
    repository_root = adapter_root.parents[1]
    adapter = tomllib.loads(
        (adapter_root / "pyproject.toml").read_text(encoding="utf-8")
    )
    core = tomllib.loads(
        (repository_root / "pyproject.toml").read_text(encoding="utf-8")
    )

    adapter_dependencies = adapter["project"]["dependencies"]
    assert any(item.startswith("cad3d-ir>=") for item in adapter_dependencies)
    assert any(item.startswith("cadquery-ocp-novtk>=") for item in adapter_dependencies)
    assert all("ocp" not in item.casefold() for item in core["project"]["dependencies"])
