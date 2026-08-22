"""Bounded STEP Part 21 header inspection used before XDE transfer."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_HEADER_LIMIT = 2 * 1024 * 1024
_SCHEMA_BLOCK = re.compile(
    r"FILE_SCHEMA\s*\(\s*\((.*?)\)\s*\)\s*;",
    re.IGNORECASE | re.DOTALL,
)
_QUOTED_VALUE = re.compile(r"'((?:''|[^'])*)'")


@dataclass(frozen=True, slots=True)
class StepHeader:
    """The small header subset needed by the AP242 profile gate."""

    is_part21: bool
    schemas: tuple[str, ...]

    @property
    def is_ap242(self) -> bool:
        """Return whether any declared schema is an AP242 schema."""
        return any("AP242" in schema.upper() for schema in self.schemas)


def inspect_step_header(path: Path) -> StepHeader:
    """Inspect a bounded prefix without invoking the geometry parser."""
    with path.open("rb") as stream:
        prefix = stream.read(_HEADER_LIMIT)
    text = prefix.decode("latin-1", errors="replace")
    match = _SCHEMA_BLOCK.search(text)
    schemas: tuple[str, ...] = ()
    if match is not None:
        schemas = tuple(
            value.replace("''", "'").strip()
            for value in _QUOTED_VALUE.findall(match.group(1))
        )
    return StepHeader(
        is_part21="ISO-10303-21" in text.upper(),
        schemas=schemas,
    )
