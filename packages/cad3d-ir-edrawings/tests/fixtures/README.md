# Fixtures (eDrawings 2026 writer)

Self-authored EPRT/EASM files saved by eDrawings 2026 (HOOPS Exchange STEP
import, HOOPS 3DF 20.10 records behind a 19.10 header) from STEP sources the
project generated or owns. No third-party CAD data is included; the same files
ship with edrawings-kit's test suite.

| File | Source | Known truth |
| --- | --- | --- |
| `box-100x50x20mm.eprt` | OCCT `BRepPrimAPI_MakeBox(100, 50, 20)`, STEP in millimetres | one body as six face shells, bounds 0–100 × 0–50 × 0–20, `model_units=1` |
| `box-100x50x20mm-as-m.eprt` | the same box written in metres | bounds 0–0.1 × 0–0.05 × 0–0.02, `model_units=1000` |
| `box-100x50x20mm-as-inch.eprt` | the same box written in inches | bounds 0–3.937 × 0–1.9685 × 0–0.7874, `model_units=25.4` |
| `reference-assembly.easm` | cad3d-ir-step reference assembly (plate 40 × 20 × 2 with two Ø4 × 10 pins) | components `Base Plate` and `Pin` (two placements at x = 10 and 30), bounds 0–40 × 0–20 × 0–12 |
| `japanese-names-assembly.easm` | the same assembly with XCAF names `ベース板`, `ピン_φ4` | the component names in UTF-8 and as sign-extended Unicode Options |

The three boxes establish the unit rule: coordinates stay in the model's own
units and axes, `model_units` (millimetres per unit) scales them, and all three
land on the same 100 × 50 × 20 mm package bounds.
