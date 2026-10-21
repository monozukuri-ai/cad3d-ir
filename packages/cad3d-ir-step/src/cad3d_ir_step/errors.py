"""Public exceptions raised by the STEP adapter."""


class StepImportError(ValueError):
    """Base class for deterministic STEP import failures."""


class StepReadError(StepImportError):
    """Raised when OCCT cannot read or transfer a STEP or IGES document."""


class UnsupportedStepSchemaError(StepImportError):
    """Raised when AP242 was required but another STEP schema was found."""


class UnsupportedIRVersionError(StepImportError):
    """Raised when the adapter cannot produce the requested IR version."""
