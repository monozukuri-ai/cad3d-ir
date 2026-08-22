"""Public exceptions raised by the STEP adapter."""


class StepImportError(ValueError):
    """Base class for deterministic STEP import failures."""


class StepReadError(StepImportError):
    """Raised when OCCT cannot read or transfer a STEP document."""


class UnsupportedStepSchemaError(StepImportError):
    """Raised when strict mode receives a non-AP242 STEP schema."""


class UnsupportedIRVersionError(StepImportError):
    """Raised when the adapter cannot produce the requested IR version."""
