"""Autodesk Inventor part (IPT) importer for cad3d-ir through inventor-kit."""

from cad3d_ir_inventor.errors import (
    InventorExportError,
    InventorImportError,
    InventorReadError,
    UnsupportedIRVersionError,
)
from cad3d_ir_inventor.export import InventorStepResult, export_step
from cad3d_ir_inventor.importer import (
    ADAPTER_NAME,
    ADAPTER_VERSION,
    SIGNATURE,
    BodyRecord,
    Classification,
    InventorImporter,
    InventorSource,
    PreviewImage,
    classify,
    inventor_kit_version,
    is_compound_file,
    map_source,
    read_source,
    release_of_major,
)

__version__ = ADAPTER_VERSION

__all__ = [
    "ADAPTER_NAME",
    "ADAPTER_VERSION",
    "SIGNATURE",
    "BodyRecord",
    "Classification",
    "InventorExportError",
    "InventorImportError",
    "InventorImporter",
    "InventorReadError",
    "InventorSource",
    "InventorStepResult",
    "PreviewImage",
    "UnsupportedIRVersionError",
    "__version__",
    "classify",
    "export_step",
    "inventor_kit_version",
    "is_compound_file",
    "map_source",
    "read_source",
    "release_of_major",
]
