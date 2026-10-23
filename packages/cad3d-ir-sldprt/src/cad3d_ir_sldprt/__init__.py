"""SOLIDWORKS part (SLDPRT) importer for cad3d-ir through sldkit display meshes."""

from cad3d_ir_sldprt.display import DisplayGlb, build_display_glb
from cad3d_ir_sldprt.errors import (
    SldprtImportError,
    SldprtReadError,
    UnsupportedDocumentKindError,
    UnsupportedIRVersionError,
)
from cad3d_ir_sldprt.importer import (
    ADAPTER_VERSION,
    SldprtImporter,
    SldprtSource,
    read_source,
    solidworks_year_estimate,
)
from cad3d_ir_sldprt.preview import PreviewImage, preview_image

__version__ = ADAPTER_VERSION

__all__ = [
    "DisplayGlb",
    "PreviewImage",
    "SldprtImportError",
    "SldprtImporter",
    "SldprtReadError",
    "SldprtSource",
    "UnsupportedDocumentKindError",
    "UnsupportedIRVersionError",
    "__version__",
    "build_display_glb",
    "preview_image",
    "read_source",
    "solidworks_year_estimate",
]
