"""Build minimal modern-container SolidWorks documents without any CAD file.

The frame and table grammar mirror what sldkit's own tests use: a modern
chunk envelope holding the ``swXmlContents/Features`` header and one
``Contents/DisplayLists`` stream with two face tables (a quad with a red face
colour and an uncoloured triangle). ``face_tessellation_assembly_document``
builds the SOLIDWORKS 2023+ layout instead: ``FaceTessellations/Directory``
plus one section stream, and a ``COMPINSTANCETREE`` with nested references.
"""

from __future__ import annotations

import hashlib
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


NO_SECTION = 0xFFFFFFFF


def _record_id(path: str) -> bytes:
    return hashlib.blake2b(path.encode("utf-8"), digest_size=8).digest()


def _directory_record(
    path: str, reference: bytes, ident: bytes, stream: str, section: int, bodies: int
) -> bytes:
    out = mfc_string(path) + struct.pack("<I", 100) + bytes(8) + reference + ident
    out += mfc_string(stream) + struct.pack("<II", section, bodies)
    for body in range(bodies):
        out += struct.pack("<III", 101 + body, 80, 6)
    return out


def _face(points: list[tuple[float, float, float]], reference: str) -> bytes:
    header = struct.pack("<2I", len(points) - 2, 1)
    trailer = bytes(8) + struct.pack("<I", 1) + bytes(80) + utf16(reference)
    return header + table(points) + trailer


def face_tessellation_assembly_document(
    instances: list[dict],
    *,
    assembly: str = "Frame",
    subassemblies: list[dict] | None = None,
) -> bytes:
    """A SOLIDWORKS 2023-style assembly saved with ``FaceTessellations``.

    ``instances`` are top-level parts with ``name``, ``number``, optional
    ``transform`` (sixteen tokens), ``hidden``, ``suppressed`` and either
    ``points`` (one face table; ``None`` for an occurrence without tables) or
    ``repeat_of`` = ``(name, number)`` of the owner whose tables it repeats.
    ``subassemblies`` carry ``name``, ``number``, ``transform`` and
    ``children`` with the same keys; ``omit_reference=True`` leaves a child out
    of the component tree so its placement cannot be resolved. ``tree_name``
    renames an item in the component tree only (the directory keeps ``name``),
    and ``tree_only=True`` lists an item in the component tree but not in the
    directory.
    """
    subassemblies = subassemblies or []
    files = [f'<swFile id="0" swDocType="ASSEMBLY" swPath="C:\\{assembly}.SLDASM"/>']
    models: list[str] = []
    file_ids: dict[str, int] = {}

    def file_id(name: str, kind: str) -> int:
        if name not in file_ids:
            file_ids[name] = len(file_ids) + 1
            files.append(
                f'<swFile id="{file_ids[name]}" swDocType="{kind}" '
                f'swPath="C:\\{name}.SLD{"ASM" if kind == "ASSEMBLY" else "PRT"}"/>'
            )
        return file_ids[name]

    def reference(item: dict, model_ref: str, sw_id: int) -> str:
        return (
            f'<swReference swName="{item.get("tree_name", item["name"])}" '
            f'swReferenceNumber="{item["number"]}" swID="{sw_id}" '
            f'swModelRef="{model_ref}" '
            f'swTransform="{item.get("transform", IDENTITY_TOKENS)}" '
            f'swSuppressed="{int(bool(item.get("suppressed")))}" '
            f'swHidden="{int(bool(item.get("hidden")))}"/>'
        )

    part_models: dict[str, str] = {}

    def part_model(name: str) -> str:
        if name not in part_models:
            ref = file_id(name, "PART")
            part_models[name] = f"m{ref}"
            models.append(
                f'<swModel id="m{ref}" swFileRef="{ref}" '
                'swConfigurationName="Default"/>'
            )
        return part_models[name]

    def tree_name(item: dict) -> str:
        return str(item.get("tree_name", item["name"]))

    sw_id = 100
    root_references: list[str] = []
    for item in instances:
        sw_id += 1
        root_references.append(reference(item, part_model(tree_name(item)), sw_id))
    for sub in subassemblies:
        sw_id += 1
        sub_model = f"s{file_id(tree_name(sub), 'ASSEMBLY')}"
        root_references.append(reference(sub, sub_model, sw_id))
        children = []
        for child in sub["children"]:
            if child.get("omit_reference"):
                continue
            sw_id += 1
            children.append(reference(child, part_model(tree_name(child)), sw_id))
        models.append(
            f'<swModel id="{sub_model}" '
            f'swFileRef="{file_id(tree_name(sub), "ASSEMBLY")}" '
            'swConfigurationName="Default">' + "".join(children) + "</swModel>"
        )
    xml = (
        "<root><swHeader>" + "".join(files) + "</swHeader><swModelList>"
        '<swModel id="self" swFileRef="0" swConfigurationId="0">'
        '<swConfiguration swID="0" swName="Default"/>'
        + "".join(root_references)
        + "</swModel>"
        + "".join(models)
        + "</swModelList></root>"
    )

    # Directory records in tree order; a sub-assembly node consumes an index.
    stream_name = "000-000-001"
    records: list[tuple[int, bytes]] = []
    sections: list[tuple[bytes, list[bytes]]] = []
    owner_ids: dict[tuple[str, str, int], bytes] = {}
    tree_index = 0

    def add(parent: str, prefix: str, item: dict) -> None:
        nonlocal tree_index
        if item.get("tree_only"):
            return
        tree_index += 1
        path = f"{prefix}{item['name']}-{item['number']}@{parent}"
        ident = _record_id(path)
        points = item.get("points")
        repeat_of = item.get("repeat_of")
        if points is not None:
            owner_ids[(parent, item["name"], item["number"])] = ident
            section = len(sections)
            sections.append((ident, [_face(points, "moSimpleSurfIdRep_c,7,1, ")]))
            record = _directory_record(path, bytes(8), ident, stream_name, section, 1)
        elif repeat_of is not None:
            owner = owner_ids[(parent, repeat_of[0], repeat_of[1])]
            record = _directory_record(path, owner, ident, "", NO_SECTION, 1)
        else:
            record = _directory_record(path, bytes(8), ident, "", NO_SECTION, 0)
        records.append((tree_index, record))

    for item in instances:
        add(assembly, "", item)
    for sub in subassemblies:
        tree_index += 1  # the sub-assembly node itself
        for child in sub["children"]:
            add(sub["name"], f"{sub['name']}-{sub['number']}@{assembly}/", child)

    directory = struct.pack("<4I", 2, 1, len(records), records[0][0] if records else 1)
    for position, (index, record) in enumerate(records):
        if position:
            directory += struct.pack("<I", index)
        directory += record
    stream = b""
    for ordinal, (ident, faces) in enumerate(sections):
        stream += struct.pack("<I", ordinal) + ident + struct.pack("<I", len(faces))
        stream += b"".join(faces) + struct.pack("<I", 7)

    environment = class_declaration(b"uoTempAssemblySHDData_c") + mfc_string(assembly)
    return (
        document_header("ASSEMBLY")
        + modern_frame("swXmlContents/COMPINSTANCETREE", xml.encode())
        + modern_frame("Contents/DisplayLists", environment)
        + modern_frame("FaceTessellations/Directory", directory)
        + modern_frame(f"FaceTessellations/{stream_name}", stream)
    )
