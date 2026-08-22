from importlib.metadata import metadata, version

import cad3d_ir


def test_distribution_metadata_matches_public_version() -> None:
    distribution = metadata("cad3d-ir")

    assert version("cad3d-ir") == cad3d_ir.__version__
    assert distribution["Requires-Python"] == ">=3.10"
    assert distribution["License-Expression"] == "MIT"
