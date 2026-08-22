"""Generate the repository-owned AP242/XDE abstraction fixture."""

from __future__ import annotations

import argparse
import re
from collections.abc import Sequence
from pathlib import Path

from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
from OCP.gp import gp_Trsf, gp_Vec
from OCP.IFSelect import IFSelect_RetDone
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


def _set_name(label: object, name: str) -> None:
    TDataStd_Name.Set_s(label, TCollection_ExtendedString(name))


def _location(x: float, y: float, z: float) -> TopLoc_Location:
    transform = gp_Trsf()
    transform.SetTranslationPart(gp_Vec(x, y, z))
    return TopLoc_Location(transform)


def generate(output: Path) -> None:
    """Write a small reusable-part assembly as STEP AP242."""
    output.parent.mkdir(parents=True, exist_ok=True)
    STEPControl_Controller.Init_s()
    if not Interface_Static.SetCVal_s("write.step.schema", "AP242DIS"):
        raise RuntimeError("OCCT rejected the AP242DIS STEP schema setting")

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
    args = parser.parse_args(argv)
    generate(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
