"""Generate the repository-owned XDE fixtures (STEP AP242, STEP AP214, IGES)."""

from __future__ import annotations

import argparse
import re
from collections.abc import Sequence
from pathlib import Path

from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
from OCP.gp import gp_Trsf, gp_Vec
from OCP.IFSelect import IFSelect_RetDone
from OCP.IGESCAFControl import IGESCAFControl_Writer
from OCP.IGESControl import IGESControl_Controller
from OCP.Interface import Interface_Static
from OCP.Quantity import Quantity_Color, Quantity_TOC_RGB
from OCP.STEPCAFControl import STEPCAFControl_Writer
from OCP.STEPControl import STEPControl_Controller, STEPControl_StepModelType
from OCP.TCollection import TCollection_ExtendedString
from OCP.TDataStd import TDataStd_Name
from OCP.TDocStd import TDocStd_Document
from OCP.TopLoc import TopLoc_Location
from OCP.XCAFApp import XCAFApp_Application
from OCP.XCAFDoc import XCAFDoc_ColorType, XCAFDoc_DocumentTool

_FIXED_TIMESTAMP = "2000-01-01T00:00:00"
_FILE_NAME_TIMESTAMP = re.compile(
    r"(FILE_NAME\s*\(\s*'(?:''|[^'])*'\s*,\s*)'(?:''|[^'])*'",
    re.IGNORECASE,
)
_SCHEMAS = {"ap242": "AP242DIS", "ap214": "AP214CD"}


def _set_name(label: object, name: str) -> None:
    TDataStd_Name.Set_s(label, TCollection_ExtendedString(name))


def _location(x: float, y: float, z: float) -> TopLoc_Location:
    transform = gp_Trsf()
    transform.SetTranslationPart(gp_Vec(x, y, z))
    return TopLoc_Location(transform)


def _build_document() -> TDocStd_Document:
    """Build the reference assembly as an XDE document."""
    application = XCAFApp_Application.GetApplication_s()
    document = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
    application.InitDocument(document)
    shape_tool = XCAFDoc_DocumentTool.ShapeTool_s(document.Main())
    shape_tool.SetAutoNaming_s(False)
    color_tool = XCAFDoc_DocumentTool.ColorTool_s(document.Main())

    assembly_label = shape_tool.NewShape()
    _set_name(assembly_label, "Reference Assembly")

    plate_label = shape_tool.AddShape(
        BRepPrimAPI_MakeBox(40.0, 20.0, 2.0).Shape(),
        False,
    )
    _set_name(plate_label, "Base Plate")
    color_tool.SetColor(
        plate_label,
        Quantity_Color(0.2, 0.4, 0.8, Quantity_TOC_RGB),
        XCAFDoc_ColorType.XCAFDoc_ColorGen,
    )

    pin_label = shape_tool.AddShape(
        BRepPrimAPI_MakeCylinder(2.0, 10.0).Shape(),
        False,
    )
    _set_name(pin_label, "Pin")
    color_tool.SetColor(
        pin_label,
        Quantity_Color(0.8, 0.7, 0.2, Quantity_TOC_RGB),
        XCAFDoc_ColorType.XCAFDoc_ColorGen,
    )

    pin_pair_label = shape_tool.NewShape()
    _set_name(pin_pair_label, "Pin Pair")
    pin_components = (
        (pin_label, "Pin:1", (0.0, 0.0, 0.0)),
        (pin_label, "Pin:2", (20.0, 0.0, 0.0)),
    )
    for definition, name, position in pin_components:
        component = shape_tool.AddComponent(
            pin_pair_label,
            definition,
            _location(*position),
        )
        _set_name(component, name)

    root_components = (
        (plate_label, "Base Plate:1", (0.0, 0.0, 0.0)),
        (pin_pair_label, "Pin Pair:1", (10.0, 10.0, 2.0)),
    )
    for definition, name, position in root_components:
        component = shape_tool.AddComponent(
            assembly_label,
            definition,
            _location(*position),
        )
        _set_name(component, name)
    shape_tool.UpdateAssemblies()
    return document


def generate(output: Path, *, schema: str = "ap242") -> None:
    """Write the reference assembly as STEP (``ap242``/``ap214``) or IGES."""
    output.parent.mkdir(parents=True, exist_ok=True)
    if schema == "iges":
        _generate_iges(output)
        return
    STEPControl_Controller.Init_s()
    if not Interface_Static.SetCVal_s("write.step.schema", _SCHEMAS[schema]):
        raise RuntimeError(f"OCCT rejected the {_SCHEMAS[schema]} schema setting")
    document = _build_document()

    writer = STEPCAFControl_Writer()
    writer.SetNameMode(True)
    writer.SetColorMode(True)
    writer.SetLayerMode(True)
    writer.SetPropsMode(True)
    if not writer.Transfer(document, STEPControl_StepModelType.STEPControl_AsIs):
        raise RuntimeError("OCCT failed to transfer the reference XDE document")
    if writer.Write(str(output)) != IFSelect_RetDone:
        raise RuntimeError(f"OCCT failed to write {output}")

    text = output.read_text(encoding="utf-8")
    normalized, replacements = _FILE_NAME_TIMESTAMP.subn(
        rf"\g<1>'{_FIXED_TIMESTAMP}'",
        text,
        count=1,
    )
    if replacements != 1:
        raise RuntimeError("could not normalize the STEP FILE_NAME timestamp")
    output.write_text(normalized, encoding="utf-8", newline="\n")


def _generate_iges(output: Path) -> None:
    IGESControl_Controller.Init_s()
    if not Interface_Static.SetCVal_s("write.iges.unit", "MM"):
        raise RuntimeError("OCCT rejected the IGES unit setting")
    if not Interface_Static.SetIVal_s("write.iges.brep.mode", 1):
        raise RuntimeError("OCCT rejected the IGES B-Rep mode setting")
    document = _build_document()
    writer = IGESCAFControl_Writer()
    writer.SetNameMode(True)
    writer.SetColorMode(True)
    writer.SetLayerMode(True)
    if not writer.Transfer(document):
        raise RuntimeError("OCCT failed to transfer the reference XDE document")
    if not writer.Write(str(output)):
        raise RuntimeError(f"OCCT failed to write {output}")
    text = output.read_text(encoding="latin-1")
    output.write_text(_normalize_iges(text), encoding="latin-1", newline="\n")


def _hollerith(value: str) -> str:
    return f"{len(value)}H{value}"


def _split_global_fields(text: str) -> list[str]:
    """Split a global section into raw fields, keeping Hollerith prefixes."""
    fields: list[str] = []
    index = 0
    current = ""
    while index < len(text):
        digits_end = index
        while digits_end < len(text) and text[digits_end].isdigit():
            digits_end += 1
        if digits_end > index and digits_end < len(text) and text[digits_end] == "H":
            length = int(text[index:digits_end])
            end = digits_end + 1 + length
            current += text[index:end]
            index = end
            continue
        char = text[index]
        if char == ",":
            fields.append(current)
            current = ""
        elif char == ";":
            fields.append(current)
            current = ""
            break
        elif not char.isspace():
            current += char
        index += 1
    return fields


def _normalize_iges(text: str) -> str:
    """Fix dates, author and company so the fixture is byte-stable everywhere."""
    lines = text.splitlines()
    global_lines = [line for line in lines if len(line) >= 73 and line[72] == "G"]
    fields = _split_global_fields("".join(line[:72] for line in global_lines))
    fixed_date = _hollerith(
        _FIXED_TIMESTAMP.replace("-", "").replace("T", ".").replace(":", "")
    )
    # IGES global parameters (1-based): 18 = file date, 21 = author,
    # 22 = organisation, 25 = modification date.
    fields[17] = fixed_date
    fields[20] = _hollerith("cad3d-ir")
    fields[21] = _hollerith("monozukuri-ai")
    fields[24] = fixed_date
    body = ",".join(fields) + ";"
    new_global = [body[index : index + 72] for index in range(0, len(body), 72)]
    rebuilt: list[str] = []
    sequence = 0
    inserted = False
    for line in lines:
        if len(line) >= 73 and line[72] == "G":
            if not inserted:
                for chunk in new_global:
                    sequence += 1
                    rebuilt.append(f"{chunk:<72}G{sequence:07d}")
                inserted = True
            continue
        if len(line) >= 73 and line[72] == "T":
            counts = {"S": 0, "G": len(new_global), "D": 0, "P": 0}
            for section in ("S", "D", "P"):
                counts[section] = sum(
                    1 for item in rebuilt if len(item) >= 73 and item[72] == section
                )
            terminate = "".join(f"{key}{counts[key]:7d}" for key in "SGDP")
            rebuilt.append(f"{terminate:<72}T{line[73:]}")
            continue
        rebuilt.append(line)
    return "\n".join(rebuilt) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "output",
        type=Path,
        nargs="?",
        default=(
            Path(__file__).resolve().parents[1]
            / "tests"
            / "fixtures"
            / "reference-assembly.step"
        ),
    )
    parser.add_argument(
        "--schema",
        choices=sorted(_SCHEMAS) + ["iges"],
        default="ap242",
        help="STEP schema to write, or 'iges' for an IGES file",
    )
    args = parser.parse_args(argv)
    generate(args.output, schema=args.schema)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
