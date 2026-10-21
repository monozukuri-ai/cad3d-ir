"""STEP and IGES importers for cad3d-ir through Open CASCADE XDE."""

from cad3d_ir_step.errors import (
    StepImportError,
    StepReadError,
    UnsupportedIRVersionError,
    UnsupportedStepSchemaError,
)
from cad3d_ir_step.iges import IgesHeader, IgesImporter, inspect_iges_header
from cad3d_ir_step.importer import StepImporter

__version__ = "0.2.0"

__all__ = [
    "IgesHeader",
    "IgesImporter",
    "StepImportError",
    "StepImporter",
    "StepReadError",
    "UnsupportedIRVersionError",
    "UnsupportedStepSchemaError",
    "__version__",
    "inspect_iges_header",
]
