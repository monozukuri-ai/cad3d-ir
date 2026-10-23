"""Build minimal modern-container SolidWorks documents without any CAD file.

The frame and table grammar mirror what sldkit's own tests use: a modern
chunk envelope holding the ``swXmlContents/Features`` header and one
``Contents/DisplayLists`` stream with two face tables (a quad with a red face
colour and an uncoloured triangle).
"""

from __future__ import annotations

import struct
import zlib

MODERN_MARKER = bytes.fromhex("140006000800")
QUAD = [(0.0, 0.0, 0.0), (0.001, 0.0, 0.0), (0.0, 0.001, 0.0), (0.001, 0.001, 0.0)]
TRIANGLE = [(0.0, 0.0, 0.0), (0.002, 0.0, 0.0), (0.0, 0.002, 0.0)]


def modern_frame(name: str, payload: bytes) -> bytes:
    compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    compressed = compressor.compress(payload) + compressor.flush()
    encoded_name = bytes(
        ((value << 4) & 0xF0) | (value >> 4) for value in name.encode("ascii")
    )
    return b"".join(
        (
            MODERN_MARKER,
            struct.pack(
                "<IIIII",
                7,
                zlib.crc32(payload),
                len(compressed),
                len(payload),
                len(encoded_name),
            ),
            encoded_name,
            compressed,
        )
    )


def document_header(kind: str = "PART") -> bytes:
    xml = (
        f'<root><swHeader><swFile id="0" swDocType="{kind}"/></swHeader>'
        '<swModelList><swModel id="self" swFileRef="0" '
        'swConfigurationId="0"><swConfiguration swID="0" '
        'swName="Default"/></swModel></swModelList></root>'
    )
    return (
        b"SLDK"
        + (4).to_bytes(4, "big")
        + modern_frame("swXmlContents/Features", xml.encode())
    )


def channel(width: int, kind: int, count: int, data: bytes) -> bytes:
    return struct.pack("<4I", width, kind, 2, count) + data


def table(positions: list[tuple[float, float, float]], normals: bool = True) -> bytes:
    count = len(positions)
    endpoints = 2 * count - 2
    flat = [value for point in positions for value in point]
    normal_values = [0.0, 0.0, 1.0] * count if normals else []
    return b"".join(
        (
            channel(4, 8, 1, struct.pack("<I", count)),
            channel(12, 100, count, struct.pack(f"<{len(flat)}f", *flat)),
            channel(
                12,
                100,
                len(normal_values) // 3,
                struct.pack(f"<{len(normal_values)}f", *normal_values),
            ),
            channel(4, 8, endpoints, bytes(4 * endpoints)),
            channel(4, 8, 1, struct.pack("<I", endpoints)),
            channel(1, 8, endpoints, bytes(endpoints)),
        )
    )


def class_declaration(name: bytes) -> bytes:
    return b"\xff\xff\x01\x00" + struct.pack("<H", len(name)) + name


def utf16(text: str) -> bytes:
    data = text.encode("utf-16-le")
    return b"\xff\xfe\xff" + bytes([len(data) // 2]) + data


def display_lists(*, normals: bool = True) -> bytes:
    red = b"\x33\x80" + struct.pack("<I", 0x000000FF) + bytes(12) + utf16("red")
    return b"".join(
        (
            class_declaration(b"uoTempFaceTessData_c"),
            struct.pack("<2I", 2, 1),
            table(QUAD, normals),
            red,
            b"\x01\x80",
            struct.pack("<2I", 1, 1),
            table(TRIANGLE, normals),
            utf16("moSimpleSurfIdRep_c,202,7,"),
        )
    )


def synthetic_document(
    kind: str = "PART", *, display: bool = True, normals: bool = True
) -> bytes:
    payload = document_header(kind)
    if display:
        payload += modern_frame("Contents/DisplayLists", display_lists(normals=normals))
    return payload


def mfc_string(text: str) -> bytes:
    """MFC ``CString`` as stored in display streams: ``ff fe ff`` + length + UTF-16."""
    data = text.encode("utf-16-le")
    count = len(data) // 2
    prefix = bytes([count]) if count < 255 else b"\xff" + struct.pack("<H", count)
    return b"\xff\xfe\xff" + prefix + data


IDENTITY_TOKENS = "1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"


def assembly_document(
    instances: list[dict],
    *,
    assembly: str = "Frame",
    leading_points: list[tuple[float, float, float]] | None = None,
) -> bytes:
    """A modern assembly with ``swReference`` components and placed display tables.

    Each instance dict has ``name``, ``number``, optional ``transform`` (sixteen
    space-separated tokens), ``hidden``, ``suppressed`` and ``points`` (one face
    table, ``None`` for no geometry). ``leading_points`` adds a face table before
    any instance record, which no component can claim.
    """
    files = [f'<swFile id="0" swDocType="ASSEMBLY" swPath="C:\\{assembly}.SLDASM"/>']
    models = []
    references = []
    for index, item in enumerate(instances, start=1):
        name = item["name"]
        files.append(
            f'<swFile id="{index}" swDocType="PART" swPath="C:\\{name}.SLDPRT"/>'
        )
        models.append(
            f'<swModel id="m{index}" swFileRef="{index}" '
            'swConfigurationName="Default"/>'
        )
        number = item["number"]
        references.append(
            f'<swReference swName="{name}" swReferenceNumber="{number}" '
            f'swID="{100 + index}" swModelRef="m{index}" '
            f'swTransform="{item.get("transform", IDENTITY_TOKENS)}" '
            f'swSuppressed="{int(bool(item.get("suppressed")))}" '
            f'swHidden="{int(bool(item.get("hidden")))}"/>'
        )
    xml = (
        "<root><swHeader>" + "".join(files) + "</swHeader><swModelList>"
        '<swModel id="self" swFileRef="0" swConfigurationId="0">'
        '<swConfiguration swID="0" swName="Default"/>'
        + "".join(references)
        + "</swModel>"
        + "".join(models)
        + "</swModelList></root>"
    )
    # sldkit reads components (swReference) from the COMPINSTANCETREE stream only;
    # the Features stream carries the document kind.
    header = document_header("ASSEMBLY") + modern_frame(
        "swXmlContents/COMPINSTANCETREE", xml.encode()
    )
    chunks = [class_declaration(b"uoTempAssemblySHDData_c"), mfc_string(assembly)]
    first = True

    def face_table(points: list[tuple[float, float, float]]) -> bytes:
        nonlocal first
        if first:
            first = False
            return b"".join(
                (
                    class_declaration(b"uoTempFaceTessData_c"),
                    struct.pack("<2I", 2, 1),
                    table(points),
                )
            )
        return b"".join((b"\x01\x80", struct.pack("<2I", 1, 1), table(points)))

    if leading_points is not None:
        chunks.append(face_table(leading_points))
    for item in instances:
        if item.get("points") is None:
            continue
        chunks.append(mfc_string(f"{item['name']}-{item['number']}@{assembly}"))
        chunks.append(face_table(item["points"]))
    return header + modern_frame("Contents/DisplayLists", b"".join(chunks))
