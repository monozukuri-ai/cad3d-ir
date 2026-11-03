from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest
from cad3d_ir import DirectoryResourceWriter, validate_package
from cad3d_ir.importers import Importer, ImportOptions
from cad3d_ir.mesh import read_glb, validate_glb_bytes

from cad3d_ir_parasolid import (
    ParasolidImporter,
    ParasolidReadError,
    UnsupportedIRVersionError,
    common_header,
    is_parasolid_signature,
    read_source,
    transform_matrix,
)

# The Onshape box is 10 x 20 x 30 mm (0.01 x 0.02 x 0.03 m in the file).
BOX_EXTENT = (10.0, 20.0, 30.0)
ONSHAPE_BLUE = (0.615686274509804, 0.811764705882353, 0.929411764705882)
# The reference's disc is a sheet of radius 1 m with the colour (0.5, 0.25, 1).
DISC_COLOUR = (0.5, 0.25, 1.0)


def _json_chunk(payload: bytes) -> dict:
    length = struct.unpack_from("<I", payload, 12)[0]
    return json.loads(payload[20 : 20 + length].decode("utf-8"))


def _import(path: Path, output: Path, **overrides):
    options = ImportOptions(
        include_visualization_mesh=True, include_brep=False, strict=False
    )
    if overrides:
        options = ImportOptions(
            **{**{f: getattr(options, f) for f in options.__slots__}, **overrides}
        )
    return ParasolidImporter().import_file(
        path, options=options, resources=DirectoryResourceWriter(output)
    )


def _visualization_payload(result, output: Path) -> bytes:
    (visualization,) = result.package.visualizations
    resource = next(
        r for r in result.package.resources if r.id == visualization.resource_id
    )
    return (output / resource.uri).read_bytes()


def test_importer_implements_core_protocol() -> None:
    assert isinstance(ParasolidImporter(), Importer)
    assert ParasolidImporter().format_name == "x_t"
    assert ParasolidImporter().suffixes == {".x_t", ".x_b", ".xmt_txt", ".xmt_bin"}


def test_probe_uses_the_header_line(box_path: Path, tmp_path: Path) -> None:
    importer = ParasolidImporter()
    assert importer.probe(box_path) == pytest.approx(0.9)
    renamed = tmp_path / "model.bin"
    renamed.write_bytes(box_path.read_bytes())
    assert importer.probe(renamed) == pytest.approx(0.6)
    other = tmp_path / "notes.x_t"
    other.write_text("ISO-10303-21;\nHEADER;\n", encoding="ascii")
    assert importer.probe(other) == 0.0
    assert importer.probe(tmp_path / "missing.x_t") == 0.0
    assert is_parasolid_signature(box_path.read_bytes()[:64])
    # Streams without the common header (authored fixtures) are accepted too.
    assert is_parasolid_signature(b"T51 : TRANSMIT FILE created by modeller")
    assert is_parasolid_signature(b"PS\0\0")
    assert not is_parasolid_signature(b"ISO-10303-21;")


def test_common_header_names_the_writer(box_path: Path) -> None:
    header = common_header(box_path.read_bytes()[:4096])
    assert header["APPL"] == "Onshape"
    assert header["FRU"] == "Parasolid 37.1.212"
    assert header["SCH"] == "SCH_3701212_37102"
    assert common_header(b"nothing here") == {}


def test_box_imports_as_one_part_with_one_coloured_body(
    box_path: Path, tmp_path: Path
) -> None:
    output = tmp_path / "out"
    result = _import(box_path, output)
    package = result.package
    validate_package(package, resource_root=output)
    (product,) = package.product_definitions
    assert product.kind == "part" and product.name == "onshape-box-v30"
    assert product.properties["application"] == "Onshape"
    (configuration,) = package.configurations
    (body,) = package.bodies
    assert configuration.body_ids == [body.id] and not package.occurrences
    assert body.name == "M1 Box"
    assert body.properties["colour"] == pytest.approx(ONSHAPE_BLUE)
    assert body.properties["status"] == "complete" and body.properties["faces"] == 6
    assert len(package.roots) == 1
    payload = _visualization_payload(result, output)
    assert validate_glb_bytes(payload).valid
    scene = read_glb(payload)
    assert scene.placed_triangle_count == 12
    assert scene.placed_line_segment_count == 12
    gltf = _json_chunk(payload)
    assert [node["name"] for node in gltf["nodes"][1:]] == ["M1 Box"]
    assert gltf["nodes"][1]["extras"]["id"] == body.id
    colours = [
        m["pbrMetallicRoughness"]["baseColorFactor"][:3] for m in gltf["materials"]
    ]
    assert any(colour == pytest.approx(ONSHAPE_BLUE) for colour in colours)
    (visualization,) = package.visualizations
    assert visualization.statistics["triangles"] == 12
    bounds = visualization.bounds
    assert bounds is not None
    for axis, size in enumerate(BOX_EXTENT):
        assert bounds.max[axis] - bounds.min[axis] == pytest.approx(size, abs=1e-6)
    codes = [d.code for d in result.diagnostics]
    assert "PARASOLID_EXACT_BREP" in codes and "PARASOLID_UNIT_M" in codes
    assert "PARASOLID_COLOURS_FROM_ATTRIBUTES" in codes
    assert "PARASOLID_ATTRIBUTES_NOT_MAPPED" in codes
    assert "PARASOLID_NO_GEOMETRY" not in codes
    assert "PARASOLID_SCHEMA_FAMILY_MATCH" not in codes
    unmapped = next(
        d for d in result.diagnostics if d.code == "PARASOLID_ATTRIBUTES_NOT_MAPPED"
    )
    assert "BTI/DetId" in unmapped.details["identifiers"]
    statistics = result.statistics
    assert statistics["schema_key"] == "SCH_3000000_30000"
    assert statistics["schema_profile"] == "onshape-sch30000-r3"
    assert statistics["family_match"] is False
    assert statistics["application"] == "Onshape"
    assert statistics["parasolid_version"] == "Parasolid 37.1.212"
    assert statistics["bodies"] == 1 and statistics["bodies_converted"] == 1
    assert statistics["faces"] == 6 and statistics["faces_dropped"] == 0
    assert statistics["coloured_bodies"] == 1 and statistics["named_bodies"] == 1
    assert statistics["triangles"] == 12 and statistics["encoding"] == "text"
    assert package.source_documents[0].version == "Parasolid 37.1.212"
    assert package.source_documents[0].properties["header"]["APPL"] == "Onshape"
    assert package.metadata["adapter"] == "cad3d-ir-parasolid"


def test_binary_and_text_boxes_agree(
    box_path: Path, box_binary_path: Path, tmp_path: Path
) -> None:
    text = _import(box_path, tmp_path / "text")
    binary = _import(box_binary_path, tmp_path / "binary")
    assert binary.statistics["encoding"] == "binary"
    assert binary.statistics["triangles"] == text.statistics["triangles"] == 12
    assert binary.package.bodies[0].name == text.package.bodies[0].name == "M1 Box"
    # X_T writes the coordinates in decimal, X_B as doubles: equal to rounding.
    (text_bounds,) = [v.bounds for v in text.package.visualizations]
    (binary_bounds,) = [v.bounds for v in binary.package.visualizations]
    assert text_bounds is not None and binary_bounds is not None
    assert binary_bounds.min == pytest.approx(text_bounds.min, abs=1e-9)
    assert binary_bounds.max == pytest.approx(text_bounds.max, abs=1e-9)


def test_brep_resources_are_written_on_request(box_path: Path, tmp_path: Path) -> None:
    output = tmp_path / "out"
    result = _import(box_path, output, include_brep=True)
    validate_package(result.package, resource_root=output)
    (representation,) = result.package.representations
    assert representation.kind == "brep" and representation.fidelity == "exact"
    assert result.package.bodies[0].representation_ids == [representation.id]
    resource = next(
        r for r in result.package.resources if r.id == representation.resource_id
    )
    assert resource.uri.endswith(".brep")
    assert (output / resource.uri).read_bytes().startswith(b"DBRep_DrawableShape")


def test_assembly_instances_become_occurrences(
    assembly_path: Path, tmp_path: Path
) -> None:
    output = tmp_path / "out"
    result = _import(assembly_path, output)
    package = result.package
    validate_package(package, resource_root=output)
    kinds = {p.id: p.kind for p in package.product_definitions}
    assert sorted(kinds.values()) == ["assembly", "assembly", "assembly", "part"]
    (root,) = package.roots
    assert kinds[root.product_id] == "assembly"
    root_configuration = next(
        c for c in package.configurations if c.id == root.configuration_id
    )
    assert len(root_configuration.occurrence_ids) == 1
    assert root_configuration.body_ids == []
    by_id = {o.id: o for o in package.occurrences}
    assert len(by_id) == 4
    placed = next(
        o for o in by_id.values() if o.id.startswith("occurrence-instance-0-")
    )
    assert placed.transform.matrix[0][:3] == pytest.approx((0.0, -1.0, 0.0))
    assert placed.transform.matrix[1][:3] == pytest.approx((1.0, 0.0, 0.0))
    assert [row[3] for row in placed.transform.matrix[:3]] == pytest.approx(
        (1000.0, 2000.0, 3000.0)
    )
    negative = next(o for o in by_id.values() if o.properties.get("negative"))
    assert kinds[negative.product_id] == "assembly"
    (body,) = package.bodies
    assert body.properties["coloured_faces"] == 1
    payload = _visualization_payload(result, output)
    assert validate_glb_bytes(payload).valid
    scene = read_glb(payload)
    (visualization,) = package.visualizations
    assert visualization.statistics["triangles"] > 0
    assert scene.placed_triangle_count == 2 * visualization.statistics["triangles"]
    gltf = _json_chunk(payload)
    assert len(gltf["meshes"]) == 1
    assert len(gltf["nodes"]) == 5  # root, assembly, two instances, nested instance
    colours = [
        m["pbrMetallicRoughness"]["baseColorFactor"][:3] for m in gltf["materials"]
    ]
    assert any(colour == pytest.approx(DISC_COLOUR) for colour in colours)
    codes = {d.code for d in result.diagnostics}
    assert {"PARASOLID_ASSEMBLY_STRUCTURE", "PARASOLID_NEGATIVE_INSTANCE"} <= codes
    statistics = result.statistics
    assert statistics["assemblies"] == 2 and statistics["instances"] == 3
    assert statistics["negative_instances"] == 1 and statistics["occurrences"] == 4
    assert statistics["products"] == 4 and statistics["unique_meshes"] == 1
    assert statistics["coloured_faces"] == 1
    assert statistics["schema_key"] == "SCH_1300000_13006"


def test_transform_matrix_scales_the_translation_to_millimetres(
    assembly_path: Path,
) -> None:
    source = read_source(assembly_path, convert=False)
    transform = source.parsed.brep.instances[0].transform
    expected = (
        (0.0, -1.0, 0.0, 1000.0),
        (1.0, 0.0, 0.0, 2000.0),
        (0.0, 0.0, 1.0, 3000.0),
        (0.0, 0.0, 0.0, 1.0),
    )
    for row, expected_row in zip(transform_matrix(transform), expected, strict=True):
        assert row == pytest.approx(expected_row)
    assert transform_matrix(None)[3] == (0.0, 0.0, 0.0, 1.0)


def test_invalid_shape_is_shown_with_a_warning(
    cylinder_path: Path, tmp_path: Path
) -> None:
    # OCCT's BRepCheck rejects the converted cylinder with a hole; parasolid-kit
    # returns it under partial conversion and the adapter shows it as built.
    output = tmp_path / "out"
    result = _import(cylinder_path, output)
    (body,) = result.package.bodies
    assert body.name == "M5 Cylinder Hole"
    assert body.properties["colour"] == pytest.approx(ONSHAPE_BLUE)
    codes = {d.code for d in result.diagnostics}
    assert "PARASOLID_SHAPE_INVALID" in codes and "PARASOLID_NO_GEOMETRY" not in codes
    assert result.statistics["bodies_converted"] == 1
    assert result.statistics["triangles"] > 0
    assert validate_glb_bytes(_visualization_payload(result, output)).valid


def test_unsupported_schema_key_fails_closed(
    unsupported_path: Path, tmp_path: Path
) -> None:
    result = _import(unsupported_path, tmp_path / "out")
    assert not result.package.visualizations and not result.package.bodies
    (diagnostic,) = [
        d for d in result.diagnostics if d.code == "PARASOLID_SCHEMA_UNSUPPORTED"
    ]
    assert diagnostic.severity == "error"
    assert diagnostic.details["application"] == "Onshape"
    assert diagnostic.details["code"] == "schema.missing_base_schema"
    assert result.statistics["schema_unsupported"] is True
    assert result.statistics["parse_failed"] is True
    # The stream's key (what parasolid-kit matched), not the common header's
    # SCH= line (Onshape's native SCH_3701212_37102)
    assert result.statistics["schema_key"] == "SCH_3000000_30900"
    assert "SCH_3000000_30900" in diagnostic.message
    assert "PARASOLID_NO_GEOMETRY" not in {d.code for d in result.diagnostics}
    with pytest.raises(ParasolidReadError, match="unsupported schema key"):
        _import(unsupported_path, tmp_path / "strict", strict=True)


def test_rejects_other_ir_versions_and_non_parasolid_files(
    box_path: Path, tmp_path: Path
) -> None:
    with pytest.raises(UnsupportedIRVersionError):
        ParasolidImporter().import_file(
            box_path,
            options=ImportOptions(ir_version="0.0.1"),
            resources=DirectoryResourceWriter(tmp_path / "out"),
        )
    with pytest.raises(ParasolidReadError):
        ParasolidImporter().import_file(
            tmp_path / "missing.x_t",
            options=ImportOptions(),
            resources=DirectoryResourceWriter(tmp_path / "out"),
        )
    other = tmp_path / "other.x_t"
    other.write_bytes(b"not a transmit file" * 20)
    with pytest.raises(ParasolidReadError, match="not a Parasolid"):
        ParasolidImporter().import_file(
            other,
            options=ImportOptions(),
            resources=DirectoryResourceWriter(tmp_path / "out2"),
        )
