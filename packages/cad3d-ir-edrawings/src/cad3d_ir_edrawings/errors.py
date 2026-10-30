"""Public exceptions raised by the eDrawings adapter."""


class EdrawingsImportError(ValueError):
    """Base class for deterministic eDrawings import failures."""


class EdrawingsReadError(EdrawingsImportError):
    """Raised when the source is not a readable eDrawings document."""


class EdrawingsLimitError(EdrawingsImportError):
    """Raised when edrawings-kit stops at one of the configured resource limits."""


class UnknownSelectionError(EdrawingsImportError):
    """Raised when the requested configuration or display state does not exist."""


class UnsupportedDocumentKindError(EdrawingsImportError):
    """Raised for eDrawings drawings (``.edrw``) unless they are accepted."""


class UnsupportedIRVersionError(EdrawingsImportError):
    """Raised when the adapter cannot produce the requested IR version."""
