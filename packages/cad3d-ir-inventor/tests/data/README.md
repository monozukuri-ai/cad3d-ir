# Test data

`box-10-20-30.ipt` and `ring-r10-r4-h20.ipt` were authored with inventor-kit's
part writer (`inventor_kit.new_part(...).add_body(...).saveas(...)`) from
CadQuery solids; no file produced by Autodesk Inventor is committed.

- `box-10-20-30.ipt`: a 10 x 20 x 30 mm box, default output profile
  (segment major 31), title `Box 10 x 20 x 30`, part number `BOX-001`.
- `ring-r10-r4-h20.ipt`: a cylinder of radius 10 mm and height 20 mm with a
  through hole of radius 4 mm, `target_year=2026` (segment major 30), title
  `Cylinder with a hole`, part number `RING-001`.

`make_fixtures.py` writes both; run it in an environment with
`inventor-kit[cadquery]`:

```sh
python make_fixtures.py OUT_DIR
```

The writer stores no preview image, so the preview path is tested with a
stand-in document.
