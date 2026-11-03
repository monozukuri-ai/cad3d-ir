"""Parasolid transmit-file (X_T / X_B) importer for cad3d-ir through parasolid-kit."""

from cad3d_ir_parasolid.errors import (
    ParasolidExportError,
    ParasolidImportError,
    ParasolidReadError,
    UnsupportedIRVersionError,
)
from cad3d_ir_parasolid.export import ParasolidStepResult, export_step
from cad3d_ir_parasolid.importer import (
    ADAPTER_NAME,
    ADAPTER_VERSION,
    SIGNATURE,
    BodyRecord,
    Classification,
    ParasolidImporter,
    ParasolidLimits,
    ParasolidSource,
    attribute_colour,
    attribute_name,
    classify,
    common_header,
    is_parasolid_signature,
    map_source,
    parasolid_kit_version,
    placements,
    read_source,
    transform_matrix,
)

__version__ = ADAPTER_VERSION

__all__ = [
    "ADAPTER_NAME",
    "ADAPTER_VERSION",
    "SIGNATURE",
    "BodyRecord",
    "Classification",
    "ParasolidExportError",
    "ParasolidImportError",
    "ParasolidImporter",
    "ParasolidLimits",
    "ParasolidReadError",
    "ParasolidSource",
    "ParasolidStepResult",
    "UnsupportedIRVersionError",
    "__version__",
    "attribute_colour",
    "attribute_name",
    "classify",
    "common_header",
    "export_step",
    "is_parasolid_signature",
    "map_source",
    "parasolid_kit_version",
    "placements",
    "read_source",
    "transform_matrix",
]
