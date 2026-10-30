"""The preview image eDrawings stores next to the model, checked and sized.

eDrawings containers carry a ``preview.jpg`` entry (older files may hold a PNG
or a Windows bitmap). The bytes are kept as they are; this module only checks
the signature, reads the pixel dimensions and bounds them.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Final

JPEG_SIGNATURE: Final = b"\xff\xd8"
PNG_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"
BMP_SIGNATURE: Final = b"BM"
MAX_PREVIEW_PIXELS: Final = 50_000_000
# Start-of-frame markers (SOF0..SOF15 without the DHT, JPG and DAC markers).
_SOF_MARKERS: Final = frozenset(
    {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
)
_STANDALONE_MARKERS: Final = frozenset({0x01, 0xD8, *range(0xD0, 0xD8)})


@dataclass(frozen=True, slots=True)
class PreviewImage:
    """One preview: a complete JPEG, PNG or BMP file in ``data``."""

    media_type: str
    encoding: str
    data: bytes
    width: int
    height: int
    source_kind: str

    @property
    def suffix(self) -> str:
        return {"jpeg": "jpg", "png": "png"}.get(self.encoding, "bmp")


def jpeg_image(data: bytes, *, source_kind: str = "preview_jpeg") -> PreviewImage:
    """Read the frame dimensions of a JPEG and return it unchanged."""
    if len(data) < 4 or data[:2] != JPEG_SIGNATURE:
        raise ValueError("invalid JPEG header")
    position = 2
    while position + 4 <= len(data):
        if data[position] != 0xFF:
            raise ValueError("invalid JPEG marker")
        marker = data[position + 1]
        if marker == 0xFF:
            position += 1
            continue
        if marker in _STANDALONE_MARKERS:
            position += 2
            continue
        if marker in {0xD9, 0xDA}:
            break
        (length,) = struct.unpack_from(">H", data, position + 2)
        if length < 2:
            raise ValueError("invalid JPEG segment length")
        if marker in _SOF_MARKERS:
            if position + 9 > len(data):
                raise ValueError("truncated JPEG frame header")
            _precision, height, width = struct.unpack_from(">BHH", data, position + 4)
            _check_dimensions(width, height)
            return PreviewImage("image/jpeg", "jpeg", data, width, height, source_kind)
        position += 2 + length
    raise ValueError("JPEG has no frame header")


def png_image(data: bytes, *, source_kind: str = "preview_png") -> PreviewImage:
    """Validate a PNG header and return it unchanged."""
    if len(data) < 24 or data[:8] != PNG_SIGNATURE or data[12:16] != b"IHDR":
        raise ValueError("invalid PNG header")
    width, height = struct.unpack_from(">II", data, 16)
    _check_dimensions(width, height)
    return PreviewImage("image/png", "png", data, width, height, source_kind)


def bmp_image(data: bytes, *, source_kind: str = "preview_bmp") -> PreviewImage:
    """Validate a BMP file header and return it unchanged."""
    if len(data) < 26 or data[:2] != BMP_SIGNATURE:
        raise ValueError("invalid BMP header")
    (file_size,) = struct.unpack_from("<I", data, 2)
    (header_size,) = struct.unpack_from("<I", data, 14)
    if file_size > len(data) or header_size < 12:
        raise ValueError("truncated BMP")
    if header_size == 12:
        width, height = struct.unpack_from("<hh", data, 18)
    else:
        width, height = struct.unpack_from("<ii", data, 18)
    _check_dimensions(width, abs(height))
    return PreviewImage("image/bmp", "bmp", data, width, abs(height), source_kind)


def preview_image(data: bytes, media_type: str | None = None) -> PreviewImage:
    """Convert preview bytes; the signature decides, ``media_type`` is a hint."""
    kind = (media_type or "").lower()
    source_kind = f"preview_{kind.rsplit('/', 1)[-1]}" if kind else "preview"
    if data[:2] == JPEG_SIGNATURE:
        return jpeg_image(data, source_kind=source_kind)
    if data[:8] == PNG_SIGNATURE:
        return png_image(data, source_kind=source_kind)
    if data[:2] == BMP_SIGNATURE:
        return bmp_image(data, source_kind=source_kind)
    raise ValueError(f"unsupported preview image {media_type or 'bytes'!r}")


def _check_dimensions(width: int, height: int) -> None:
    if width <= 0 or height <= 0 or width * height > MAX_PREVIEW_PIXELS:
        raise ValueError("preview dimensions are out of range")


__all__ = [
    "MAX_PREVIEW_PIXELS",
    "PreviewImage",
    "bmp_image",
    "jpeg_image",
    "png_image",
    "preview_image",
]
