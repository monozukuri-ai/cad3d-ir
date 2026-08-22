"""Stable high-level API surface."""

from cad3d_ir.io import (
    dump_manifest,
    dumps_manifest,
    load_manifest,
    loads_manifest,
    manifest_dict,
)
from cad3d_ir.schema import load_schema
from cad3d_ir.validation import validate_package

__all__ = [
    "dump_manifest",
    "dumps_manifest",
    "load_manifest",
    "load_schema",
    "loads_manifest",
    "manifest_dict",
    "validate_package",
]
