"""Physical source-document and dependency provenance."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, JsonValue

from cad3d_ir.model.common import Identifier, NamedObject, Sha256


class SourceDependency(NamedObject):
    """A dependency declared by one physical source document."""

    uri: str = Field(min_length=1)
    kind: Literal[
        "component",
        "drawing-model",
        "derived",
        "embedded",
        "unknown",
    ] = "unknown"
    target_document_id: Identifier | None = None
    required: bool | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class SourceDocument(NamedObject):
    """A physical source asset represented by this package."""

    format: str = Field(min_length=1)
    version: str | None = None
    uri: str | None = None
    sha256: Sha256 | None = None
    dependencies: list[SourceDependency] = Field(default_factory=list)
