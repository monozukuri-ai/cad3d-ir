from __future__ import annotations

import pytest

from cad3d_ir import CadPackage, IRValidationError, validate_package


def test_resource_integrity(
    sample_package: CadPackage,
    package_directory,
) -> None:
    validate_package(sample_package, resource_root=package_directory)


def test_resource_digest_mismatch(
    sample_package: CadPackage,
    package_directory,
) -> None:
    (package_directory / "geometry" / "bracket.step").write_bytes(b"changed")

    with pytest.raises(IRValidationError) as caught:
        validate_package(sample_package, resource_root=package_directory)

    codes = {issue.code for issue in caught.value.issues}
    assert codes == {"RESOURCE_SIZE_MISMATCH", "RESOURCE_DIGEST_MISMATCH"}
