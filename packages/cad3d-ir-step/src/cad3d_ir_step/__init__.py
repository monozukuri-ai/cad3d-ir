"""STEP and IGES importers, and a STEP writer, for cad3d-ir through Open CASCADE XDE."""

from cad3d_ir_step.direct import (
    DIRECT_BYTES_PER_TRIANGLE,
    TESSELLATED_BYTES_PER_NORMAL,
    TESSELLATED_BYTES_PER_TRIANGLE,
    TESSELLATED_BYTES_PER_VERTEX,
    DirectStepResult,
    MeshStepEstimate,
    Representation,
    estimate_mesh_step,
    write_mesh_step,
)
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

__version__ = "0.4.1"

__all__ = [
    "DEFAULT_MAX_TRIANGLES",
    "DIRECT_BYTES_PER_TRIANGLE",
    "TESSELLATED_BYTES_PER_NORMAL",
    "TESSELLATED_BYTES_PER_TRIANGLE",
    "TESSELLATED_BYTES_PER_VERTEX",
    "DirectStepResult",
    "IgesHeader",
    "IgesImporter",
    "MeshBody",
    "MeshStepEstimate",
    "MeshStepResult",
    "MeshTooLargeError",
    "Representation",
    "StepExportError",
    "StepExportResult",
    "StepImportError",
    "StepImporter",
    "StepReadError",
    "UnsupportedIRVersionError",
    "UnsupportedStepSchemaError",
    "__version__",
    "estimate_mesh_step",
    "export_step",
    "inspect_iges_header",
    "mesh_to_step",
    "write_mesh_step",
]
