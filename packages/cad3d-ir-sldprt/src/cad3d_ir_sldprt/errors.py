"""Public exceptions raised by the SOLIDWORKS part adapter."""


class SldprtImportError(ValueError):
    """Base class for deterministic SLDPRT import failures."""


class SldprtReadError(SldprtImportError):
    """Raised when the source is not a readable SolidWorks document."""


class UnsupportedDocumentKindError(SldprtImportError):
    """Raised for drawings, and for assemblies unless they are accepted."""


class UnsupportedIRVersionError(SldprtImportError):
    """Raised when the adapter cannot produce the requested IR version."""
