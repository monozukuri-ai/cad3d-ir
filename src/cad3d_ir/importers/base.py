"""Protocol implemented by optional source-format adapter packages."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite, pi
from pathlib import Path
from typing import Protocol, runtime_checkable

from cad3d_ir.constants import CURRENT_IR_VERSION
from cad3d_ir.diagnostics import ImportResult
from cad3d_ir.io.resources import ResourceWriter


@dataclass(frozen=True, slots=True)
class ImportOptions:
    """Format-neutral behavior requested from an importer.

    ``include_visualization_mesh`` asks for derived display meshes: one
    ``mesh`` representation per body and one package-level
    :class:`~cad3d_ir.model.Visualization` (glTF binary). ``include_brep``
    controls whether exact kernel B-Rep resources are written when the
    adapter has them. The ``mesh_*`` values are tessellation targets: the
    linear deflection is the given ratio of the root bounding-box diagonal and
    the angular deflection is in radians.
    """

    ir_version: str = CURRENT_IR_VERSION
    strict: bool = True
    validate: bool = True
    include_visualization_mesh: bool = False
    include_brep: bool = True
    mesh_linear_deflection_ratio: float = 0.001
    mesh_angular_deflection: float = 0.3
    mesh_include_edges: bool = True

    def __post_init__(self) -> None:
        for name in ("mesh_linear_deflection_ratio", "mesh_angular_deflection"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise ValueError(f"{name} must be a finite positive number")
            if not isfinite(value) or value <= 0.0:
                raise ValueError(f"{name} must be a finite positive number")
        if self.mesh_angular_deflection > pi:
            raise ValueError("mesh_angular_deflection must not exceed pi radians")


@runtime_checkable
class Importer(Protocol):
    """The minimal boundary between the core and a format adapter."""

    format_name: str
    suffixes: frozenset[str]

    def probe(self, path: Path) -> float:
        """Return a confidence in ``[0, 1]`` without fully parsing the source."""
        ...

    def import_file(
        self,
        path: Path,
        *,
        options: ImportOptions,
        resources: ResourceWriter,
    ) -> ImportResult:
        """Map a source-native model directly to the common IR."""
        ...
