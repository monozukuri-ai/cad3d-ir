"""Build minimal eDrawings inputs in memory, without any CAD file.

The HSF grammar is the one edrawings-kit's own tests use: a 19.10 header,
``(`` / ``)`` segments with one-byte name lengths, ``~`` face colours as RGB
bytes, ``%`` modelling matrices as twelve floats (row-vector convention, the
translation last), ``S`` shells with 8-bit face lists, ``<`` includes and ``U``
user options. Bodies under an ``include library`` segment are drawn only through
their includes, which is how eDrawings shares one body between placements.
"""

from __future__ import annotations

import io
import struct
import zipfile

HEADER = b";; HSF V19.10 \n"
TERMINATOR = b"\0"
# A 2 x 3 x 4 box and a 1 x 1.5 x 4 pin (half the box in x and y).
BOX = (
    (0, 0, 0),
    (2, 0, 0),
    (2, 3, 0),
    (0, 3, 0),
    (0, 0, 4),
    (2, 0, 4),
    (2, 3, 4),
    (0, 3, 4),
)
PIN = tuple((x * 0.5, y * 0.5, z) for x, y, z in BOX)
FACES = (
    *(4, 0, 3, 2, 1),
    *(4, 4, 5, 6, 7),
    *(4, 0, 1, 5, 4),
    *(4, 3, 7, 6, 2),
    *(4, 0, 4, 7, 3),
    *(4, 1, 2, 6, 5),
)
RED = (255, 0, 0)
BLUE = (0, 0, 255)


def opened(name: str) -> bytes:
    data = name.encode("utf-8")
    return b"(" + bytes([len(data)]) + data


CLOSE = b")"


def shell(points=BOX, faces=FACES) -> bytes:
    values = struct.pack(f"<{len(faces)}B", *faces)
    record = b"S" + bytes([16]) + b"\0" + struct.pack("<i", len(points))
    record += b"".join(struct.pack("<3f", *point) for point in points)
    return record + b"\x01" + struct.pack("<i", len(values) + 1) + bytes([8]) + values


def matrix(values) -> bytes:
    """``%`` modelling matrix: three rotation rows then the translation row."""
    return b"%" + struct.pack("<12f", *values)


def translation(x: float, y: float, z: float) -> bytes:
    return matrix((1, 0, 0, 0, 1, 0, 0, 0, 1, x, y, z))


def color(rgb) -> bytes:
    return b"~\x01" + bytes(rgb)


def option(value: str) -> bytes:
    raw = value.encode("utf-8")
    return b"U" + struct.pack("<H", len(raw)) + raw


def include(path: str) -> bytes:
    raw = path.encode("utf-8")
    return b"<" + bytes([len(raw)]) + raw


FACES_OFF = b"V\x01\0"
"""Visibility record with the face bit cleared: eDrawings hides the segment."""


def model_hsf(
    *,
    units: float | None = 1.0,
    hidden_pin: bool = False,
    body: bytes = HEADER,
) -> bytes:
    """A red plate at the origin and a shared blue pin placed at x = 10 and 30.

    ``units`` writes the ``model_units`` option (millimetres per model unit);
    ``None`` omits it, which the adapter reads as metres. ``hidden_pin`` adds a
    third pin at x = 50 whose segment carries the face-visibility-off flag.
    """
    stream = body
    if units is not None:
        stream += opened("includes") + option(f"model_units={units:.6f}") + CLOSE
    stream += opened("plate") + color(RED) + shell() + CLOSE
    stream += opened("include library") + opened("pin") + shell(PIN) + CLOSE + CLOSE
    for name, x in (("pin-1", 10.0), ("pin-2", 30.0)):
        stream += (
            opened(name)
            + translation(x, 0, 0)
            + color(BLUE)
            + include("/include library/pin")
            + CLOSE
        )
    if hidden_pin:
        stream += (
            opened("pin-3")
            + translation(50.0, 0, 0)
            + color(BLUE)
            + FACES_OFF
            + include("/include library/pin")
            + CLOSE
        )
    return stream + TERMINATOR


def configurations_hsf() -> bytes:
    """Two saved configurations with one body each (``config0`` is shown first)."""

    def config(segment: str, name: str, rgb) -> bytes:
        return (
            opened(segment)
            + option(f"config name=`{name}`")
            + opened("v")
            + color(rgb)
            + shell()
            + CLOSE
            + CLOSE
        )

    return (
        HEADER
        + config("config1", "Beta", BLUE)
        + config("config0", "Alpha", RED)
        + TERMINATOR
    )


def empty_hsf() -> bytes:
    """A stream with segments and text options but no shell: nothing to show."""
    return HEADER + opened("sheet0") + option("name=`Sheet1`") + CLOSE + TERMINATOR


def tiny_jpeg(width: int = 4, height: int = 3) -> bytes:
    """A JPEG prefix with APP0 and a baseline frame header; enough to be sized."""
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\0\x01\x01\0\0\x01\0\x01\0\0"
    sof0 = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\0"
    return b"\xff\xd8" + app0 + sof0 + b"\xff\xd9"


def zip_container(hsf: bytes, *, preview: bytes | None = tiny_jpeg()) -> bytes:
    """An eDrawings ZIP container: ``eModel`` first, then ``preview.jpg``."""
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("eModel", hsf)
        if preview is not None:
            archive.writestr("preview.jpg", preview)
    return stream.getvalue()


PREAMBLE_LENGTH = 92


def prefixed_container(zip_bytes: bytes) -> bytes:
    """The observed 92-byte preamble (``2008+ archive``) before the ZIP."""
    preamble = b"\x5b\x01\x00\x00\x00\xbe\x0d" + b"2008+ archive"
    preamble += bytes(PREAMBLE_LENGTH - 4 - len(preamble)) + b"\xff\xff\xff\x7f"
    assert len(preamble) == PREAMBLE_LENGTH
    return preamble + zip_bytes


def foreign_zip() -> bytes:
    """A ZIP whose first entry is not ``eModel`` (an Office-like package)."""
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
    return stream.getvalue()


__all__ = [
    "BLUE",
    "BOX",
    "FACES",
    "FACES_OFF",
    "HEADER",
    "PIN",
    "RED",
    "color",
    "configurations_hsf",
    "empty_hsf",
    "foreign_zip",
    "include",
    "matrix",
    "model_hsf",
    "opened",
    "option",
    "prefixed_container",
    "shell",
    "tiny_jpeg",
    "translation",
    "zip_container",
]
