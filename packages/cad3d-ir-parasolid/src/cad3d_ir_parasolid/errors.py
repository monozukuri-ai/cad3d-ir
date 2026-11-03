"""Public exceptions raised by the Parasolid adapter."""


class ParasolidImportError(ValueError):
    """Base class for deterministic X_T / X_B import failures."""


class ParasolidReadError(ParasolidImportError):
    """Raised when the source is not a readable Parasolid transmit file."""


class UnsupportedIRVersionError(ParasolidImportError):
    """Raised when the adapter cannot produce the requested IR version."""


class ParasolidExportError(ParasolidImportError):
    """Raised when a Parasolid model cannot be written as STEP."""
