"""Write every synthetic part as STEP: python -m tests.geometry.parts <dir>"""

import sys
from pathlib import Path

from tests.geometry.parts import PARTS, write_step

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
for name, build in PARTS.items():
    print(write_step(build(), out / f"{name}.step"))
