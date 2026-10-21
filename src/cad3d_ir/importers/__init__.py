"""Adapter protocols plus the dependency-free built-in mesh importer.

Kernel-backed CAD importers (STEP, IGES, native formats) are separate
distributions; see ``cad3d-ir-step``.
"""

from cad3d_ir.importers.base import Importer, ImportOptions
from cad3d_ir.importers.mesh import MeshImporter, MeshImportError

__all__ = ["Importer", "ImportOptions", "MeshImportError", "MeshImporter"]
