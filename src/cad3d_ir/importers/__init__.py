"""Adapter protocols; concrete CAD importers are separate distributions."""

from cad3d_ir.importers.base import Importer, ImportOptions

__all__ = ["Importer", "ImportOptions"]
