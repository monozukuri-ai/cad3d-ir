from __future__ import annotations

import struct

import pytest

from cad3d_ir_edrawings.preview import bmp_image, jpeg_image, png_image, preview_image
from tests.synthetic import tiny_jpeg


def _png(width: int, height: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
        + bytes(16)
    )


def _bmp(width: int, height: int) -> bytes:
    dib = struct.pack("<IiiHHIIiiII", 40, width, height, 1, 24, 0, 0, 0, 0, 0, 0)
    pixels = bytes(((width * 24 + 31) // 32) * 4 * abs(height))
    size = 14 + len(dib) + len(pixels)
    return struct.pack("<2sIHHI", b"BM", size, 0, 0, 14 + len(dib)) + dib + pixels


def test_jpeg_frame_dimensions_are_read() -> None:
    image = jpeg_image(tiny_jpeg(640, 480))
    assert (image.width, image.height) == (640, 480)
    assert image.media_type == "image/jpeg" and image.suffix == "jpg"
    assert image.data == tiny_jpeg(640, 480)
    with pytest.raises(ValueError, match="JPEG header"):
        jpeg_image(b"\x89PNG not a jpeg")
    with pytest.raises(ValueError, match="no frame header"):
        jpeg_image(b"\xff\xd8\xff\xd9")
    with pytest.raises(ValueError, match="out of range"):
        jpeg_image(tiny_jpeg(0, 10))


def test_png_and_bmp_previews_are_validated() -> None:
    image = png_image(_png(4, 2))
    assert (image.width, image.height, image.suffix) == (4, 2, "png")
    with pytest.raises(ValueError, match="PNG header"):
        png_image(b"not a png at all, not at all")
    image = bmp_image(_bmp(3, -2))
    assert (image.width, image.height, image.media_type) == (3, 2, "image/bmp")
    assert image.suffix == "bmp"
    with pytest.raises(ValueError, match="BMP header"):
        bmp_image(b"BX" + bytes(30))


def test_preview_image_dispatches_on_the_signature() -> None:
    assert preview_image(tiny_jpeg(), "image/jpeg").source_kind == "preview_jpeg"
    assert preview_image(_png(1, 1), None).source_kind == "preview"
    assert preview_image(_bmp(1, 1), "image/bmp").encoding == "bmp"
    with pytest.raises(ValueError, match="unsupported preview image"):
        preview_image(b"GIF89a" + bytes(16), "image/gif")
