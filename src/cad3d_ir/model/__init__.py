"""Public typed manifest models."""

from cad3d_ir.model.common import (
    BBox3,
    CoordinateSystem,
    Identifier,
    IRObject,
    SourceRef,
    Transform,
    UnitSystem,
)
from cad3d_ir.model.geometry import (
    Appearance,
    Body,
    GeometryRepresentation,
    Material,
    RepresentationDerivation,
    Resource,
)
from cad3d_ir.model.package import CadPackage
from cad3d_ir.model.product import (
    Configuration,
    Occurrence,
    ProductDefinition,
    RootSelection,
)
from cad3d_ir.model.source import SourceDependency, SourceDocument

__all__ = [
    "Appearance",
    "BBox3",
    "Body",
    "CadPackage",
    "Configuration",
    "CoordinateSystem",
    "GeometryRepresentation",
    "IRObject",
    "Identifier",
    "Material",
    "Occurrence",
    "ProductDefinition",
    "RepresentationDerivation",
    "Resource",
    "RootSelection",
    "SourceDependency",
    "SourceDocument",
    "SourceRef",
    "Transform",
    "UnitSystem",
]
