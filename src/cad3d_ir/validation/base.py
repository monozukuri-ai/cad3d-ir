"""Validation issue and exception contracts."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """One stable, path-addressed manifest validation failure."""

    code: str
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message} [{self.code}]"


class IRValidationError(ValueError):
    """Raised when structural or resource graph invariants fail."""

    def __init__(self, issues: list[ValidationIssue] | tuple[ValidationIssue, ...]):
        self.issues = tuple(issues)
        detail = "\n".join(str(issue) for issue in self.issues)
        super().__init__(detail)
