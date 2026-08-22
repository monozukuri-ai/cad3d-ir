"""Structured importer diagnostics and result contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from cad3d_ir.model import CadPackage

DiagnosticSeverity = Literal["info", "warning", "error"]
DiagnosticAction = Literal[
    "preserved",
    "normalized",
    "approximated",
    "skipped",
    "unresolved",
    "repaired",
]


@dataclass(frozen=True, slots=True)
class ImportDiagnostic:
    """One stable diagnostic emitted by a source-format adapter."""

    code: str
    severity: DiagnosticSeverity
    message: str
    action: DiagnosticAction | None = None
    object_id: str | None = None
    source_document_id: str | None = None
    source_id: str | None = None
    details: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dictionary without absent fields."""
        result: dict[str, Any] = {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
        }
        for key in (
            "action",
            "object_id",
            "source_document_id",
            "source_id",
            "details",
        ):
            value = getattr(self, key)
            if value is not None:
                result[key] = value
        return result


@dataclass(slots=True)
class ImportResult:
    """A converted package plus explicit loss and conversion statistics."""

    package: CadPackage
    diagnostics: list[ImportDiagnostic] = field(default_factory=list)
    statistics: dict[str, Any] = field(default_factory=dict)

    @property
    def warnings(self) -> list[str]:
        """Compatibility view of warning and error messages."""
        return [
            diagnostic.message
            for diagnostic in self.diagnostics
            if diagnostic.severity in {"warning", "error"}
        ]
