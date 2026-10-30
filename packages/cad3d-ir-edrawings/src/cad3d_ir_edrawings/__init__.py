"""eDrawings (EPRT/EASM) importer for cad3d-ir through edrawings-kit display bundles."""

from cad3d_ir_edrawings.display import (
    DisplayGlb,
    GlbPlacement,
    build_display_glb,
    instance_matrix,
)
from cad3d_ir_edrawings.errors import (
    EdrawingsImportError,
    EdrawingsLimitError,
    EdrawingsReadError,
    UnknownSelectionError,
    UnsupportedDocumentKindError,
    UnsupportedIRVersionError,
)
from cad3d_ir_edrawings.importer import (
    ADAPTER_VERSION,
    DEFAULT_SCALE_MM_PER_UNIT,
    EdrawingsImporter,
    EdrawingsSource,
    limits_for,
    read_source,
    sniff,
)
from cad3d_ir_edrawings.preview import PreviewImage, preview_image

__version__ = ADAPTER_VERSION

__all__ = [
    "DEFAULT_SCALE_MM_PER_UNIT",
    "DisplayGlb",
    "EdrawingsImportError",
    "EdrawingsImporter",
    "EdrawingsLimitError",
    "EdrawingsReadError",
    "EdrawingsSource",
    "GlbPlacement",
    "PreviewImage",
    "UnknownSelectionError",
    "UnsupportedDocumentKindError",
    "UnsupportedIRVersionError",
    "__version__",
    "build_display_glb",
    "instance_matrix",
    "limits_for",
    "preview_image",
    "read_source",
    "sniff",
]
