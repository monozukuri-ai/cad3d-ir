"""Author the IPT fixtures of cad3d-ir-inventor with inventor-kit's part writer.

Run once in an environment with ``inventor-kit[cadquery]``::

    python make_fixtures.py OUT_DIR
"""

import sys
from pathlib import Path

import cadquery as cq
import inventor_kit as ik

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)

box = cq.Workplane("XY").box(10, 20, 30, centered=False).val()
part = ik.new_part(name="Box", title="Box 10 x 20 x 30", part_number="BOX-001")
part.add_body(box, name="Box body")
part.saveas(out / "box-10-20-30.ipt")

ring = cq.Workplane("XY").circle(10).circle(4).extrude(20).val()
part = ik.new_part(
    name="Ring", title="Cylinder with a hole", part_number="RING-001", target_year=2026
)
part.add_body(ring, name="Ring body")
part.saveas(out / "ring-r10-r4-h20.ipt")
