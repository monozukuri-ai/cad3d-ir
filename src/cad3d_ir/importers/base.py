"""Protocol implemented by optional source-format adapter packages."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from cad3d_ir.constants import CURRENT_IR_VERSION
from cad3d_ir.diagnostics import ImportResult
from cad3d_ir.io.resources import ResourceWriter


@dataclass(frozen=True, slots=True)
class ImportOptions:
    """Format-neutral behavior requested from an importer."""

    ir_version: str = CURRENT_IR_VERSION
    strict: bool = True
    validate: bool = True
    include_visualization_mesh: bool = False


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
