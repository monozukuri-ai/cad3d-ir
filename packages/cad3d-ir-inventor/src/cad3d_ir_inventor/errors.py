"""Public exceptions raised by the Inventor adapter."""


class InventorImportError(ValueError):
    """Base class for deterministic IPT import failures."""


class InventorReadError(InventorImportError):
    """Raised when the source is not a readable Inventor part."""


class UnsupportedIRVersionError(InventorImportError):
    """Raised when the adapter cannot produce the requested IR version."""


class InventorExportError(InventorImportError):
    """Raised when an Inventor part cannot be written as STEP."""
