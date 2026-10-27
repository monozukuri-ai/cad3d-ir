"""iCAD SX (ICD) importer for cad3d-ir through icadkit's saved final bodies."""

from cad3d_ir_icad.errors import (
    IcadExportError,
    IcadImportError,
    IcadReadError,
    UnsupportedIRVersionError,
)
from cad3d_ir_icad.export import IcadStepResult, export_step
from cad3d_ir_icad.importer import (
    ADAPTER_NAME,
    ADAPTER_VERSION,
    IcadImporter,
    IcadLimits,
    IcadSource,
    PartClassification,
    classify,
    icadkit_version,
    is_icd_signature,
    map_source,
    read_source,
    version_label,
)

__version__ = ADAPTER_VERSION

__all__ = [
    "ADAPTER_NAME",
    "ADAPTER_VERSION",
    "IcadExportError",
    "IcadImportError",
    "IcadImporter",
    "IcadLimits",
    "IcadReadError",
    "IcadSource",
    "IcadStepResult",
    "PartClassification",
    "UnsupportedIRVersionError",
    "__version__",
    "classify",
    "export_step",
    "icadkit_version",
    "is_icd_signature",
    "map_source",
    "read_source",
    "version_label",
]
