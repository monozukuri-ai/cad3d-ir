from __future__ import annotations

import struct

import pytest

from cad3d_ir_sldprt.preview import dib_image, png_image, preview_image


def _png(width: int, height: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
        + bytes(16)
    )


def test_png_preview_is_validated_and_kept() -> None:
    image = png_image(_png(4, 2))
    assert (image.width, image.height, image.media_type) == (4, 2, "image/png")
    assert image.suffix == "png" and image.data == _png(4, 2)
    with pytest.raises(ValueError, match="PNG header"):
        png_image(b"not a png at all, not at all")
    with pytest.raises(ValueError, match="out of range"):
        png_image(_png(0, 2))


def test_dib_preview_gets_a_bmp_file_header() -> None:
    header = struct.pack("<IiiHHIIiiII", 40, 2, 2, 1, 24, 0, 0, 0, 0, 0, 0)
    pixels = bytes(16)  # two rows of two 24-bit pixels, each row padded to 8
    image = dib_image(header + pixels)
    assert image.media_type == "image/bmp" and image.suffix == "bmp"
    assert image.data[:2] == b"BM"
    file_size, _, _, offset = struct.unpack_from("<IHHI", image.data, 2)
    assert file_size == len(header) + len(pixels) + 14
    assert offset == 40 + 14
    assert image.data[14:] == header + pixels

    paletted = struct.pack("<IiiHHIIiiII", 40, 1, 1, 1, 8, 0, 0, 0, 0, 2, 0)
    image = dib_image(paletted + bytes(8) + bytes(4))
    assert struct.unpack_from("<I", image.data, 10)[0] == 14 + 40 + 8
    with pytest.raises(ValueError, match="truncated DIB pixels"):
        dib_image(header + pixels[:-1])
    with pytest.raises(ValueError, match="unsupported DIB layout"):
        dib_image(struct.pack("<IiiHHIIiiII", 40, 2, 2, 2, 24, 0, 0, 0, 0, 0, 0))
    with pytest.raises(ValueError, match="unsupported preview kind"):
        preview_image(b"", "preview_jpeg")
