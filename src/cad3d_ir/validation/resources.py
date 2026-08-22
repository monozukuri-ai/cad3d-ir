"""On-disk resource integrity validation."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

from cad3d_ir.model import CadPackage
from cad3d_ir.validation.base import IRValidationError, ValidationIssue


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_resources(package: CadPackage, root: str | Path) -> None:
    """Check resource confinement, existence, byte length, and SHA-256."""
    root_path = Path(root).resolve()
    issues: list[ValidationIssue] = []

    for index, resource in enumerate(package.resources):
        path = f"resources[{index}]"
        candidate = (root_path / resource.uri).resolve()
        try:
            candidate.relative_to(root_path)
        except ValueError:
            issues.append(
                ValidationIssue(
                    code="RESOURCE_OUTSIDE_PACKAGE",
                    path=f"{path}.uri",
                    message=f"resource resolves outside package root: {resource.uri!r}",
                )
            )
            continue

        if not candidate.is_file():
            issues.append(
                ValidationIssue(
                    code="RESOURCE_MISSING",
                    path=f"{path}.uri",
                    message=f"resource file does not exist: {resource.uri!r}",
                )
            )
            continue

        actual_size = candidate.stat().st_size
        if actual_size != resource.byte_length:
            issues.append(
                ValidationIssue(
                    code="RESOURCE_SIZE_MISMATCH",
                    path=f"{path}.byte_length",
                    message=(
                        f"declared {resource.byte_length} bytes, found {actual_size}"
                    ),
                )
            )

        actual_digest = _file_sha256(candidate)
        if actual_digest != resource.sha256:
            issues.append(
                ValidationIssue(
                    code="RESOURCE_DIGEST_MISMATCH",
                    path=f"{path}.sha256",
                    message=(f"declared {resource.sha256}, computed {actual_digest}"),
                )
            )

    if issues:
        raise IRValidationError(issues)
