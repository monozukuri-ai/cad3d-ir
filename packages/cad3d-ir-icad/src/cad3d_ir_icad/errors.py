"""Public exceptions raised by the iCAD SX adapter."""


class IcadImportError(ValueError):
    """Base class for deterministic ICD import failures."""


class IcadReadError(IcadImportError):
    """Raised when the source is not a readable iCAD SX document."""


class UnsupportedIRVersionError(IcadImportError):
    """Raised when the adapter cannot produce the requested IR version."""


class IcadExportError(IcadImportError):
    """Raised when an ICD model cannot be written as STEP."""
