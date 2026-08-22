from __future__ import annotations

from hashlib import sha256

import pytest

from cad3d_ir import DirectoryResourceWriter


def test_directory_resource_writer_streams_and_hashes(tmp_path) -> None:
    writer = DirectoryResourceWriter(tmp_path)

    resource = writer.write_chunks(
        id="resource-streamed",
        uri="geometry/streamed.brep",
        chunks=(b"first", b"-second"),
        media_type="application/octet-stream",
        encoding="occt-brep",
    )

    payload = b"first-second"
    assert (tmp_path / resource.uri).read_bytes() == payload
    assert resource.byte_length == len(payload)
    assert resource.sha256 == sha256(payload).hexdigest()


def test_directory_resource_writer_does_not_overwrite(tmp_path) -> None:
    writer = DirectoryResourceWriter(tmp_path)
    options = {
        "id": "resource-test",
        "uri": "geometry/test.brep",
        "data": b"first",
        "media_type": "application/octet-stream",
        "encoding": "occt-brep",
    }
    writer.write_bytes(**options)

    with pytest.raises(FileExistsError):
        writer.write_bytes(**options)

    assert (tmp_path / "geometry" / "test.brep").read_bytes() == b"first"


def test_directory_resource_writer_rejects_invalid_chunks(tmp_path) -> None:
    writer = DirectoryResourceWriter(tmp_path)

    with pytest.raises(TypeError, match="must be bytes"):
        writer.write_chunks(
            id="resource-invalid",
            uri="geometry/invalid.brep",
            chunks=(b"valid", "not-bytes"),  # type: ignore[arg-type]
            media_type="application/octet-stream",
            encoding="occt-brep",
        )

    assert not (tmp_path / "geometry" / "invalid.brep").exists()


def test_directory_resource_writer_validates_metadata_before_writing(tmp_path) -> None:
    writer = DirectoryResourceWriter(tmp_path)

    with pytest.raises(ValueError):
        writer.write_bytes(
            id="not a valid id",
            uri="geometry/invalid-id.brep",
            data=b"data",
            media_type="application/octet-stream",
            encoding="occt-brep",
        )

    assert not (tmp_path / "geometry" / "invalid-id.brep").exists()
