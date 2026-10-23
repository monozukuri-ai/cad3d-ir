"""Saved preview images (PNG or Windows DIB) as browser-displayable bytes.

SolidWorks stores a preview bitmap with every document. sldkit returns it as
PNG bytes or as a bare device-independent bitmap (DIB, no file header). The
DIB gets the 14-byte ``BITMAPFILEHEADER`` so that any image decoder can read
it; the pixel data is not touched.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Final

PNG_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"
MAX_PREVIEW_PIXELS: Final = 50_000_000
_DIB_HEADERS: Final = frozenset({40, 108, 124})


@dataclass(frozen=True, slots=True)
class PreviewImage:
    """One decoded preview: a complete PNG or BMP file in ``data``."""

    media_type: str
    encoding: str
    data: bytes
    width: int
    height: int
    source_kind: str

    @property
    def suffix(self) -> str:
        return "png" if self.encoding == "png" else "bmp"


def png_image(data: bytes, *, source_kind: str = "preview_png") -> PreviewImage:
    """Validate a PNG header and return it unchanged."""
    if len(data) < 24 or data[:8] != PNG_SIGNATURE or data[12:16] != b"IHDR":
        raise ValueError("invalid PNG header")
    width, height = struct.unpack_from(">II", data, 16)
    _check_dimensions(width, height)
    return PreviewImage("image/png", "png", data, width, height, source_kind)


def dib_image(data: bytes, *, source_kind: str = "preview_dib") -> PreviewImage:
    """Prefix a bare DIB with a BMP file header after checking its layout."""
    if len(data) < 40:
        raise ValueError("truncated DIB header")
    header, width, height, planes, depth, compression = struct.unpack_from(
        "<IiiHHI", data
    )
    if (
        header not in _DIB_HEADERS
        or header > len(data)
        or planes != 1
        or depth not in {1, 4, 8, 16, 24, 32}
        or compression not in {0, 1, 2, 3}
        or (compression == 1 and depth != 8)
        or (compression == 2 and depth != 4)
        or (compression in {1, 2} and height <= 0)
        or (compression == 3 and depth not in {16, 32})
    ):
        raise ValueError("unsupported DIB layout")
    height = abs(height)
    colors = struct.unpack_from("<I", data, 32)[0]
    if depth <= 8:
        if colors > 1 << depth:
            raise ValueError("invalid DIB palette")
        colors = colors or 1 << depth
    offset = header + colors * 4 + (12 if header == 40 and compression == 3 else 0)
    pixel_bytes = ((width * depth + 31) // 32) * 4 * height
    if compression in {1, 2}:
        pixel_bytes = struct.unpack_from("<I", data, 20)[0]
        if pixel_bytes == 0:
            raise ValueError("RLE DIB requires a declared image size")
    if offset + pixel_bytes > len(data):
        raise ValueError("truncated DIB pixels")
    _check_dimensions(width, height)
    file_header = struct.pack("<2sIHHI", b"BM", len(data) + 14, 0, 0, offset + 14)
    return PreviewImage(
        "image/bmp", "bmp", file_header + data, width, height, source_kind
    )


def preview_image(data: bytes, kind: str) -> PreviewImage:
    """Convert the bytes of a sldkit preview resource of ``kind``."""
    if kind == "preview_png":
        return png_image(data, source_kind=kind)
    if kind == "preview_dib":
        return dib_image(data, source_kind=kind)
    raise ValueError(f"unsupported preview kind {kind!r}")


def _check_dimensions(width: int, height: int) -> None:
    if width <= 0 or height <= 0 or width * height > MAX_PREVIEW_PIXELS:
        raise ValueError("preview dimensions are out of range")


__all__ = [
    "MAX_PREVIEW_PIXELS",
    "PreviewImage",
    "dib_image",
    "png_image",
    "preview_image",
]
