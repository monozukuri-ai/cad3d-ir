"""Logical products, resolved configurations, and component occurrences."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from cad3d_ir.model.common import CoreModel, Identifier, IRObject, Transform


class ProductDefinition(IRObject):
    """A reusable logical product independent of any placement."""

    kind: Literal["part", "assembly", "hybrid", "unknown"] = "unknown"
    source_document_id: Identifier | None = None
    default_configuration_id: Identifier | None = None


class Configuration(IRObject):
    """A resolved named state of one product definition."""

    product_id: Identifier
    body_ids: list[Identifier] = Field(default_factory=list)
    occurrence_ids: list[Identifier] = Field(default_factory=list)


class Occurrence(IRObject):
    """A placed instance of a product definition."""

    product_id: Identifier
    configuration_id: Identifier | None = None
    transform: Transform = Field(default_factory=Transform)
    visible: bool | None = None
    suppressed: bool | None = None


class RootSelection(CoreModel):
    """Select one top-level product and optional explicit configuration."""

    product_id: Identifier
    configuration_id: Identifier | None = None
