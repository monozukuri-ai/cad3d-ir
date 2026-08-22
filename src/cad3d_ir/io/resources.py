"""Safe streaming writes for package-relative binary resources."""

from __future__ import annotations

import os
from collections.abc import Iterable
from hashlib import sha256
from pathlib import Path
from tempfile import mkstemp
from typing import Protocol, runtime_checkable

from cad3d_ir.model import Resource, SourceRef
from cad3d_ir.model.common import ExtensionMap, PropertyMap
from cad3d_ir.model.geometry import validate_resource_uri


@runtime_checkable
class ResourceWriter(Protocol):
    """Streaming resource sink supplied by the caller to an importer."""

    def write_chunks(
        self,
        *,
        id: str,
        uri: str,
        chunks: Iterable[bytes],
        media_type: str,
        encoding: str,
        name: str | None = None,
        source: SourceRef | None = None,
        properties: PropertyMap | None = None,
        extensions: ExtensionMap | None = None,
    ) -> Resource:
        """Write bytes and return their complete manifest resource record."""
        ...


class DirectoryResourceWriter:
    """Atomically write package resources beneath one directory root."""

    def __init__(self, root: str | Path, *, overwrite: bool = False) -> None:
        self.root = Path(root).resolve()
        self.overwrite = overwrite
        self.root.mkdir(parents=True, exist_ok=True)

    def _destination(self, uri: str) -> Path:
        validate_resource_uri(uri)
        destination = self.root.joinpath(*uri.split("/"))
        destination.parent.mkdir(parents=True, exist_ok=True)
        resolved_parent = destination.parent.resolve()
        try:
            resolved_parent.relative_to(self.root)
        except ValueError as exc:
            raise ValueError(
                f"resource URI resolves outside package root: {uri!r}"
            ) from exc
        return resolved_parent / destination.name

    def write_chunks(
        self,
        *,
        id: str,
        uri: str,
        chunks: Iterable[bytes],
        media_type: str,
        encoding: str,
        name: str | None = None,
        source: SourceRef | None = None,
        properties: PropertyMap | None = None,
        extensions: ExtensionMap | None = None,
    ) -> Resource:
        """Stream a resource to a temporary file, then atomically publish it."""
        resource = Resource(
            id=id,
            name=name,
            uri=uri,
            media_type=media_type,
            encoding=encoding,
            sha256="0" * 64,
            byte_length=0,
            source=source,
            properties=properties or {},
            extensions=extensions or {},
        )
        destination = self._destination(uri)
        if destination.exists() and not self.overwrite:
            raise FileExistsError(f"resource already exists: {destination}")

        file_descriptor, temporary_name = mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
        )
        temporary_path = Path(temporary_name)
        digest = sha256()
        byte_length = 0
        try:
            with os.fdopen(file_descriptor, "wb") as stream:
                for chunk in chunks:
                    if not isinstance(chunk, bytes):
                        raise TypeError("resource chunks must be bytes")
                    stream.write(chunk)
                    digest.update(chunk)
                    byte_length += len(chunk)
            if self.overwrite:
                os.replace(temporary_path, destination)
            else:
                os.link(temporary_path, destination)
                temporary_path.unlink()
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise

        return resource.model_copy(
            update={
                "sha256": digest.hexdigest(),
                "byte_length": byte_length,
            }
        )

    def write_bytes(
        self,
        *,
        id: str,
        uri: str,
        data: bytes,
        media_type: str,
        encoding: str,
        name: str | None = None,
        source: SourceRef | None = None,
        properties: PropertyMap | None = None,
        extensions: ExtensionMap | None = None,
    ) -> Resource:
        """Write one in-memory resource through the streaming implementation."""
        return self.write_chunks(
            id=id,
            uri=uri,
            chunks=(data,),
            media_type=media_type,
            encoding=encoding,
            name=name,
            source=source,
            properties=properties,
            extensions=extensions,
        )
