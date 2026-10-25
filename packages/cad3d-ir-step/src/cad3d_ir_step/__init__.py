"""STEP and IGES importers, and a STEP writer, for cad3d-ir through Open CASCADE XDE."""

from cad3d_ir_step.errors import (
    MeshTooLargeError,
    StepExportError,
    StepImportError,
    StepReadError,
    UnsupportedIRVersionError,
    UnsupportedStepSchemaError,
)
from cad3d_ir_step.export import (
    DEFAULT_MAX_TRIANGLES,
    MeshBody,
    MeshStepResult,
    StepExportResult,
    export_step,
    mesh_to_step,
)
from cad3d_ir_step.iges import IgesHeader, IgesImporter, inspect_iges_header
from cad3d_ir_step.importer import StepImporter

__version__ = "0.3.0"

__all__ = [
    "DEFAULT_MAX_TRIANGLES",
    "IgesHeader",
    "IgesImporter",
    "MeshBody",
    "MeshStepResult",
    "MeshTooLargeError",
    "StepExportError",
    "StepExportResult",
    "StepImportError",
    "StepImporter",
    "StepReadError",
    "UnsupportedIRVersionError",
    "UnsupportedStepSchemaError",
    "__version__",
    "export_step",
    "inspect_iges_header",
    "mesh_to_step",
]
