"""Public exceptions raised by the STEP adapter and writer."""


class StepImportError(ValueError):
    """Base class for deterministic STEP import failures."""


class StepReadError(StepImportError):
    """Raised when OCCT cannot read or transfer a STEP or IGES document."""


class UnsupportedStepSchemaError(StepImportError):
    """Raised when AP242 was required but another STEP schema was found."""


class UnsupportedIRVersionError(StepImportError):
    """Raised when the adapter cannot produce the requested IR version."""


class StepExportError(ValueError):
    """Raised when a STEP file cannot be written from the given source."""


class MeshTooLargeError(StepExportError):
    """Raised when a mesh exceeds the triangle cap of the sewn-solid STEP writer."""

    def __init__(self, *, triangles: int, limit: int) -> None:
        self.triangles = triangles
        self.limit = limit
        super().__init__(
            f"the mesh has {triangles:,} triangles; the sewn-solid STEP writer "
            f"accepts at most {limit:,}. Simplify the mesh or use the tessellated mode."
        )
