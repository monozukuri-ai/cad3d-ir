"""Component placement for SOLIDWORKS assemblies from what the file already stores.

An assembly's ``DisplayLists`` stream keeps one ``uoTempPartTessData_c`` record
per component instance ahead of that instance's face tables. The record starts
with the instance path (``"<name>-<number>@<assembly>"``) as an MFC string. The
face tables themselves are stored in the component's own coordinates, so
without the placement all instances pile up at the origin.

The placement is the component's ``swTransform`` (sixteen decimal tokens that
sldkit keeps as ``AssemblyComponent.raw_transform``): a row-major 4x4 matrix in
metres whose first three rows are the rotation and whose last row is the
translation, applied to row vectors (``v' = v·R + t``). Joining the face
tables to the components by instance name and number, and applying that
matrix, reproduced the STEP export of a four-valve manifold within a tenth
of a millimetre (median nearest-neighbour distance 0.16 mm over a 418 mm
diagonal).

In a ``DisplayLists`` assembly nested subassemblies are not resolved: their
instance paths carry more than one ``@`` and their components are not listed
at the top level, so their tables stay at the stored coordinates and are
reported.

Assemblies saved by SOLIDWORKS 2023 and later keep the tables in
``FaceTessellations`` sections instead, one per referenced part, and sldkit
returns the directory of occurrences as ``mesh.instances`` (``path`` root
first, ``tables_from`` naming the owner whose tables an occurrence displays).
A top-level occurrence is placed with its component's ``swTransform``; a
nested one composes the sub-assembly's transform with the child
``swReference`` transforms that ``swXmlContents/COMPINSTANCETREE`` lists under
the sub-assembly's ``swModel``, root first.
"""

from __future__ import annotations

import bisect
import re
import struct
from dataclasses import dataclass, field
from typing import Any
from xml.etree import ElementTree

import sldkit

from cad3d_ir_sldprt.display import ColorRGBA, color_key

Matrix4 = tuple[
    tuple[float, float, float, float],
    tuple[float, float, float, float],
    tuple[float, float, float, float],
    tuple[float, float, float, float],
]

_ASSEMBLY_HEADER = b"uoTempAssemblySHDData_c"
_MFC_STRING = re.compile(rb"\xff\xfe\xff")
_INSTANCE_RE = re.compile(r"^(?P<name>.+)-(?P<number>[0-9]+)$")
_COMPONENT_TREE_PATH = "swXmlContents/COMPINSTANCETREE"
_MAX_COMPONENT_TREE_BYTES = 64 * 1024 * 1024
IDENTITY: Matrix4 = (
    (1.0, 0.0, 0.0, 0.0),
    (0.0, 1.0, 0.0, 0.0),
    (0.0, 0.0, 1.0, 0.0),
    (0.0, 0.0, 0.0, 1.0),
)


@dataclass(frozen=True, slots=True)
class InstanceRecord:
    """One ``uoTempPartTessData_c`` record found in a display stream."""

    stream_index: int
    offset: int
    path: str
    name: str
    number: int
    nested: bool
    #: Display name when ``<name>-<number>`` alone is ambiguous (nested occurrences).
    label: str | None = None


@dataclass(slots=True)
class PlacedInstance:
    """A component instance with the face tables assigned to it."""

    record: InstanceRecord
    part_indices: list[int] = field(default_factory=list)
    matrix: Matrix4 = IDENTITY
    component: Any | None = None
    placed: bool = False
    hidden: bool = False
    suppressed: bool = False

    @property
    def label(self) -> str:
        return self.record.label or f"{self.record.name}-{self.record.number}"


@dataclass(slots=True)
class AssemblyLayout:
    """The result of joining display tables to component transforms."""

    assembly_name: str | None
    instances: list[PlacedInstance]
    unassigned_parts: list[int]
    components: int
    nested_records: int
    #: ``"display_lists"`` (instance records in the stream) or
    #: ``"face_tessellations"`` (the directory of a SOLIDWORKS 2023+ assembly).
    source: str = "display_lists"
    #: Nested occurrences placed through the component tree.
    nested_placed: int = 0
    #: Occurrences whose saved name matched no component and that were placed
    #: by their position in the component tree instead.
    order_placed: int = 0

    @property
    def placed_instances(self) -> list[PlacedInstance]:
        return [item for item in self.instances if item.placed]

    @property
    def unplaced_parts(self) -> list[int]:
        """Face tables left in stored coordinates: unassigned, nested or unknown."""
        parts = set(self.unassigned_parts)
        for item in self.instances:
            if not item.placed and not item.hidden and not item.suppressed:
                parts.update(item.part_indices)
        return sorted(parts)


@dataclass(frozen=True, slots=True)
class NestedComponent:
    """One ``swReference`` under a sub-assembly's ``swModel`` (``COMPINSTANCETREE``)."""

    model_ref: str | None
    tokens: tuple[str, ...]
    hidden: bool
    suppressed: bool


@dataclass(frozen=True, slots=True)
class TreeLeaf:
    """One part occurrence of the component tree in pre-order, placed root first."""

    label: str
    name: str
    number: int
    depth: int
    matrix: Matrix4 | None
    hidden: bool
    suppressed: bool


@dataclass(slots=True)
class ComponentTree:
    """``swXmlContents/COMPINSTANCETREE``: reference index and pre-order leaves."""

    references: dict[tuple[str, str, int], NestedComponent] = field(
        default_factory=dict
    )
    leaves: list[TreeLeaf] = field(default_factory=list)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _xml_flag(value: str | None) -> bool:
    return str(value or "").strip().upper() in {"YES", "1", "TRUE"}


def parse_component_tree(data: bytes) -> ComponentTree:
    """Read the component tree XML.

    ``references`` indexes every ``swReference`` by ``(parent swModel id,
    name, number)``: the root assembly's references are there too, but sldkit
    already exposes them as components, so the index serves the nested
    levels, where the parent is the sub-assembly's ``swModel`` reached through
    ``swModelRef``. ``leaves`` lists the part occurrences in pre-order from the
    root model (the ``swModel`` of the header's first ``swFile``) with their
    composed placement, which is the order the ``FaceTessellations`` directory
    follows.
    """
    tree = ComponentTree()
    if not data or len(data) > _MAX_COMPONENT_TREE_BYTES:
        return tree
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError:
        return tree
    models: dict[str, Any] = {}
    for model in root.iter():
        if _local_name(model.tag) != "swModel":
            continue
        model_id = model.get("id")
        if model_id is None:
            continue
        models.setdefault(model_id, model)
        for reference in model:
            if _local_name(reference.tag) != "swReference":
                continue
            name = reference.get("swName")
            number = reference.get("swReferenceNumber") or ""
            if not name or not number.isdigit():
                continue
            tree.references.setdefault(
                (model_id, name, int(number)),
                NestedComponent(
                    model_ref=reference.get("swModelRef"),
                    tokens=tuple((reference.get("swTransform") or "").split()),
                    hidden=_xml_flag(reference.get("swHidden")),
                    suppressed=_xml_flag(reference.get("swSuppressed")),
                ),
            )

    def has_references(model: Any) -> bool:
        return any(_local_name(child.tag) == "swReference" for child in model)

    first_file: str | None = None
    for node in root.iter():
        if _local_name(node.tag) == "swFile":
            first_file = node.get("id")
            break
    root_model = next(
        (
            model
            for model in models.values()
            if first_file is not None
            and model.get("swFileRef") == first_file
            and has_references(model)
        ),
        None,
    ) or next((model for model in models.values() if has_references(model)), None)
    if root_model is None:
        return tree

    def walk(
        model: Any,
        prefix: str,
        depth: int,
        matrix: Matrix4 | None,
        hidden: bool,
        suppressed: bool,
        visited: frozenset[str],
    ) -> None:
        for reference in model:
            if _local_name(reference.tag) != "swReference":
                continue
            name = reference.get("swName")
            number = reference.get("swReferenceNumber") or ""
            if not name or not number.isdigit():
                continue
            own = transform_from_tokens((reference.get("swTransform") or "").split())
            composed = None if matrix is None or own is None else multiply(matrix, own)
            label = f"{prefix}{name}-{int(number)}"
            flags = (
                hidden or _xml_flag(reference.get("swHidden")),
                suppressed or _xml_flag(reference.get("swSuppressed")),
            )
            child_id = reference.get("swModelRef")
            child = models.get(child_id or "")
            if child is not None and child_id not in visited and has_references(child):
                walk(
                    child,
                    label + "/",
                    depth + 1,
                    composed,
                    flags[0],
                    flags[1],
                    visited | {child_id or ""},
                )
            else:
                tree.leaves.append(
                    TreeLeaf(
                        label=label,
                        name=name,
                        number=int(number),
                        depth=depth,
                        matrix=composed,
                        hidden=flags[0],
                        suppressed=flags[1],
                    )
                )

    walk(
        root_model,
        "",
        0,
        IDENTITY,
        False,
        False,
        frozenset({root_model.get("id") or ""}),
    )
    return tree


def component_tree(path: Any, *, limit_profile: str) -> ComponentTree:
    """Read ``swXmlContents/COMPINSTANCETREE`` from the document."""
    try:
        inventory = sldkit.inspect_file(path, profile=limit_profile)
    except sldkit.SldkitError:
        return ComponentTree()
    entries = getattr(getattr(inventory, "inventory", None), "entries", None)
    if entries is None:
        entries = getattr(inventory, "entries", []) or []
    for entry in entries:
        entry_path = str(getattr(entry, "path", "") or "").lstrip("/")
        if entry_path != _COMPONENT_TREE_PATH:
            continue
        try:
            extracted = sldkit.extract_file(
                path, entry.id, mode="decoded", profile=limit_profile
            )
        except sldkit.SldkitError:
            continue
        data = extracted.data
        if data:
            return parse_component_tree(bytes(data))
    return ComponentTree()


def mfc_strings(data: bytes) -> list[tuple[int, str]]:
    """Return ``(offset, text)`` for every MFC ``CString`` stored as UTF-16."""
    out: list[tuple[int, str]] = []
    for match in _MFC_STRING.finditer(data):
        cursor = match.end()
        if cursor >= len(data):
            continue
        length = data[cursor]
        if length == 0xFF:
            if cursor + 3 > len(data):
                continue
            length = struct.unpack_from("<H", data, cursor + 1)[0]
            cursor += 3
        else:
            cursor += 1
        if length == 0 or cursor + 2 * length > len(data):
            continue
        try:
            text = data[cursor : cursor + 2 * length].decode("utf-16-le")
        except UnicodeDecodeError:
            continue
        if any(ord(char) < 0x20 for char in text):
            continue
        out.append((match.start(), text))
    return out


def assembly_name(data: bytes) -> str | None:
    """The assembly's own name: the first MFC string after its display header."""
    at = data.find(_ASSEMBLY_HEADER)
    if at < 0:
        return None
    for offset, text in mfc_strings(data[at : at + 4096]):
        if offset > 0:
            return text
    return None


def instance_records(
    data: bytes, name: str | None, *, stream_index: int = 0
) -> list[InstanceRecord]:
    """Find the component instance records (``name-number@assembly``) in one stream."""
    records: list[InstanceRecord] = []
    for offset, text in mfc_strings(data):
        if "@" not in text:
            continue
        head, _, owner = text.rpartition("@")
        if name is not None and owner != name:
            continue
        if name is None and not owner:
            continue
        nested = "@" in head
        # Leaf first: "Bolt-2@Sub-1@Top" is Bolt-2 inside Sub-1 inside Top.
        leaf = head.partition("@")[0] if nested else head
        match = _INSTANCE_RE.match(leaf)
        if match is None:
            continue
        records.append(
            InstanceRecord(
                stream_index=stream_index,
                offset=offset,
                path=text,
                name=match.group("name"),
                number=int(match.group("number")),
                nested=nested,
            )
        )
    return records


def transform_from_tokens(tokens: Any) -> Matrix4 | None:
    """Convert sldkit's sixteen ``swTransform`` tokens to a cad3d-ir matrix in mm.

    The tokens are a row-major 4x4 for row vectors in metres. cad3d-ir applies
    matrices to column vectors in package millimetres, so the rotation block is
    transposed and the translation (last token row) moves to the last column.
    """
    try:
        values = [float(token) for token in tokens]
    except (TypeError, ValueError):
        return None
    if len(values) != 16 or any(value != value for value in values):
        return None
    rows = [values[0:4], values[4:8], values[8:12], values[12:16]]
    rotation = [[rows[column][row] for column in range(3)] for row in range(3)]
    translation = [rows[3][axis] * 1000.0 for axis in range(3)]
    return (
        (rotation[0][0], rotation[0][1], rotation[0][2], translation[0]),
        (rotation[1][0], rotation[1][1], rotation[1][2], translation[1]),
        (rotation[2][0], rotation[2][1], rotation[2][2], translation[2]),
        (0.0, 0.0, 0.0, 1.0),
    )


def multiply(left: Matrix4, right: Matrix4) -> Matrix4:
    """``left · right`` for column-vector matrices: ``right`` is applied first."""
    rows = tuple(
        tuple(sum(left[i][k] * right[k][j] for k in range(4)) for j in range(4))
        for i in range(4)
    )
    return rows  # type: ignore[return-value]


def _flag(value: Any) -> bool:
    inner = getattr(value, "value", value)
    return bool(inner) if isinstance(inner, bool) else False


def _occurrence(segment: str) -> tuple[str, int] | None:
    """``<name>-<number>`` of one ``<name>-<number>@<parent>`` path segment."""
    head = segment.rsplit("@", 1)[0] if "@" in segment else segment
    match = _INSTANCE_RE.match(head)
    if match is None:
        return None
    return match.group("name"), int(match.group("number"))


def _component_index(document: Any | None) -> dict[tuple[str, int], Any]:
    index: dict[tuple[str, int], Any] = {}
    if document is None:
        return index
    for configuration in getattr(document, "configurations", ()):
        for component in getattr(configuration, "components", ()):
            name = getattr(getattr(component, "instance_name", None), "value", None)
            number = getattr(getattr(component, "instance_number", None), "value", None)
            if (
                isinstance(name, str)
                and isinstance(number, int)
                and (name, number) not in index
            ):
                index[(name, number)] = component
    return index


def resolve_layout(
    path: Any,
    display: Any,
    document: Any | None,
    *,
    limit_profile: str,
) -> AssemblyLayout:
    """Join the display tables of an assembly to its component transforms."""
    mesh = display.mesh
    if getattr(mesh, "instances", None):
        return resolve_face_tessellation_layout(
            path, display, document, limit_profile=limit_profile
        )
    components = _component_index(document)
    instances: list[PlacedInstance] = []
    unassigned: list[int] = []
    nested_records = 0
    name: str | None = None
    for stream_index, stream in enumerate(mesh.streams):
        data = sldkit.extract_file(
            path, stream.entry_id, mode="decoded", profile=limit_profile
        ).data
        if data is None:
            continue
        data = bytes(data)
        stream_name = assembly_name(data)
        if name is None:
            name = stream_name
        records = instance_records(data, stream_name, stream_index=stream_index)
        offsets = [record.offset for record in records]
        by_record: dict[int, PlacedInstance] = {}
        for part_index, part in enumerate(mesh.parts):
            if part.stream_index != stream_index:
                continue
            slot = bisect.bisect_right(offsets, part.table_offset) - 1
            if slot < 0:
                unassigned.append(part_index)
                continue
            record = records[slot]
            instance = by_record.get(slot)
            if instance is None:
                instance = PlacedInstance(record=record)
                component = components.get((record.name, record.number))
                instance.component = component
                if record.nested:
                    nested_records += 1
                elif component is not None:
                    instance.hidden = _flag(getattr(component, "is_hidden", None))
                    instance.suppressed = _flag(
                        getattr(component, "is_suppressed", None)
                    )
                    matrix = transform_from_tokens(
                        getattr(
                            getattr(component, "raw_transform", None), "value", None
                        )
                    )
                    if matrix is not None:
                        instance.matrix = matrix
                        instance.placed = True
                by_record[slot] = instance
                instances.append(instance)
            instance.part_indices.append(part_index)
    return AssemblyLayout(
        assembly_name=name,
        instances=instances,
        unassigned_parts=unassigned,
        components=len(components),
        nested_records=nested_records,
    )


def resolve_face_tessellation_layout(
    path: Any,
    display: Any,
    document: Any | None,
    *,
    limit_profile: str,
) -> AssemblyLayout:
    """Place the occurrences of a ``FaceTessellations`` assembly (SOLIDWORKS 2023+).

    Every occurrence with tables (its own section or a repeated owner's) becomes
    an instance. Top-level occurrences take their component's ``swTransform``;
    nested ones compose it with the child references of the sub-assembly's
    ``swModel`` in ``COMPINSTANCETREE``. An occurrence whose chain is incomplete
    keeps its tables at the stored coordinates and is counted as nested.

    The directory keeps the occurrence names from the time the tessellation
    was saved, so after components were renamed no name matches. When the
    directory lists exactly the component tree's part occurrences in order
    (same count, instance numbers and nesting depth pairwise), the unmatched
    occurrences take the placement of their position in that order and are
    counted in ``order_placed``.
    """
    mesh = display.mesh
    components = _component_index(document)
    occurrences = list(mesh.instances)
    parts_by_owner: dict[int, list[int]] = {}
    unassigned: list[int] = []
    for part_index, part in enumerate(mesh.parts):
        owner = getattr(part, "instance_index", None)
        if owner is None:
            unassigned.append(part_index)
        else:
            parts_by_owner.setdefault(int(owner), []).append(part_index)
    tree: ComponentTree | None = None

    def loaded_tree() -> ComponentTree:
        nonlocal tree
        if tree is None:
            tree = component_tree(path, limit_profile=limit_profile)
        return tree

    instances: list[PlacedInstance] = []
    unmatched: list[tuple[int, PlacedInstance]] = []
    nested_records = 0
    nested_placed = 0
    name: str | None = None
    for index, occurrence in enumerate(occurrences):
        occurrence_path = str(occurrence.path)
        segments = occurrence_path.split("/")
        if name is None and "@" in segments[0]:
            name = segments[0].rsplit("@", 1)[1]
        source = getattr(occurrence, "tables_from", None)
        if source is None:
            continue
        part_indices = parts_by_owner.get(int(source), [])
        if not part_indices:
            continue
        nested = len(segments) > 1
        leaf = _occurrence(segments[-1]) or (
            str(occurrence.name),
            int(occurrence.number or 0),
        )
        owner_stream = getattr(occurrences[int(source)], "stream_index", None)
        record = InstanceRecord(
            stream_index=int(owner_stream or 0),
            offset=index,
            path=occurrence_path,
            name=leaf[0],
            number=leaf[1],
            nested=nested,
            label=(
                "/".join(segment.rsplit("@", 1)[0] for segment in segments)
                if nested
                else None
            ),
        )
        instance = PlacedInstance(record=record, part_indices=list(part_indices))
        top = _occurrence(segments[0])
        component = components.get(top) if top is not None else None
        instance.component = component
        if component is None:
            unmatched.append((index, instance))
            instances.append(instance)
            continue
        instance.hidden = _flag(getattr(component, "is_hidden", None))
        instance.suppressed = _flag(getattr(component, "is_suppressed", None))
        matrix = transform_from_tokens(
            getattr(getattr(component, "raw_transform", None), "value", None)
        )
        if nested and matrix is not None:
            nested_index = loaded_tree().references
            model_ref = getattr(component, "source_model_ref", None)
            for segment in segments[1:]:
                child = _occurrence(segment)
                found = (
                    nested_index.get((str(model_ref), child[0], child[1]))
                    if child is not None and model_ref is not None
                    else None
                )
                child_matrix = (
                    None if found is None else transform_from_tokens(found.tokens)
                )
                if found is None or child_matrix is None:
                    matrix = None
                    break
                matrix = multiply(matrix, child_matrix)
                instance.hidden = instance.hidden or found.hidden
                instance.suppressed = instance.suppressed or found.suppressed
                model_ref = found.model_ref
            if matrix is None:
                nested_records += 1
        if matrix is not None:
            instance.matrix = matrix
            instance.placed = True
            if nested:
                nested_placed += 1
        instances.append(instance)

    order_placed = 0
    if unmatched:
        leaves = loaded_tree().leaves
        aligned = len(leaves) == len(occurrences) and all(
            leaf.number == int(occurrence.number or -1)
            and leaf.depth == str(occurrence.path).count("/")
            for leaf, occurrence in zip(leaves, occurrences, strict=False)
        )
        for index, instance in unmatched:
            leaf = leaves[index] if aligned else None
            if leaf is None or leaf.matrix is None:
                if instance.record.nested:
                    nested_records += 1
                continue
            instance.matrix = leaf.matrix
            instance.placed = True
            instance.hidden = leaf.hidden
            instance.suppressed = leaf.suppressed
            order_placed += 1
            if instance.record.nested:
                nested_placed += 1
    return AssemblyLayout(
        assembly_name=name,
        instances=instances,
        unassigned_parts=unassigned,
        components=len(components),
        nested_records=nested_records,
        source="face_tessellations",
        nested_placed=nested_placed,
        order_placed=order_placed,
    )


def instance_colors(mesh: Any, instance: PlacedInstance) -> dict[ColorRGBA | None, int]:
    """Count the saved colours of one instance's face tables."""
    counts: dict[ColorRGBA | None, int] = {}
    for index in instance.part_indices:
        key = color_key(mesh.parts[index].color)
        counts[key] = counts.get(key, 0) + 1
    return counts


__all__ = [
    "AssemblyLayout",
    "ComponentTree",
    "IDENTITY",
    "InstanceRecord",
    "Matrix4",
    "NestedComponent",
    "PlacedInstance",
    "TreeLeaf",
    "assembly_name",
    "component_tree",
    "instance_colors",
    "instance_records",
    "mfc_strings",
    "multiply",
    "parse_component_tree",
    "resolve_face_tessellation_layout",
    "resolve_layout",
    "transform_from_tokens",
]
