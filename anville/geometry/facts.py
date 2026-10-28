"""PartFacts: the measured, face-level facts the planner consumes (ADR 0008 §8).

Pure Python and JSON-serializable. Fields are only ever added; a rename or removal bumps
SCHEMA_VERSION. Face IDs are OCCT face indices in STEP order, not Onshape face IDs: the
geometric signature (surface, normal, axis, radius, area, centroid) is what a connector
matches against its CAD system's faces.
"""

from dataclasses import asdict, dataclass, field

SCHEMA_VERSION = 1

# A direction points from the part toward the spindle; the tool moves along -d to cut.
# The order is the deterministic tie-break for setup search.
DIRECTIONS = ("+Z", "-Z", "+X", "-X", "+Y", "-Y")
VECTORS = {
    "+X": (1.0, 0.0, 0.0), "-X": (-1.0, 0.0, 0.0),
    "+Y": (0.0, 1.0, 0.0), "-Y": (0.0, -1.0, 0.0),
    "+Z": (0.0, 0.0, 1.0), "-Z": (0.0, 0.0, -1.0),
}

Vec = tuple[float, float, float]


@dataclass
class Access:
    """How a face is seen from one direction."""

    fraction: float           # share of the face's samples with a clear ray to the spindle
    depth: float              # mm from the stock top along d to the face's deepest point
    gap: float | None = None  # mm of clearance straight out from the face, if it is wall-like


@dataclass
class FaceFacts:
    id: int
    surface: str  # plane | cylinder | cone | sphere | torus | bspline | other
    area: float
    centroid: Vec
    stock: bool   # lies on the stock (bounding-box) surface: needs no setup
    access: dict[str, Access] = field(default_factory=dict)  # only directions with fraction > 0
    n_samples: int = 0
    normal: Vec | None = None       # planes: outward unit normal
    axis: Vec | None = None         # cylinders: axis direction
    radius: float | None = None     # cylinders: radius
    concave_radius: float | None = None  # concave cylinders (fillets, holes)
    concave_axis: str | None = None      # "X" | "Y" | "Z" when that cylinder's axis is principal


@dataclass(frozen=True)
class Edge:
    """An edge between two distinct faces."""

    a: int
    b: int
    kind: str               # tangent | concave | convex
    axis: str | None = None  # sharp concave straight edges parallel to a principal axis


@dataclass
class PartFacts:
    source_sha256: str
    bbox: tuple[Vec, Vec]
    faces: list[FaceFacts]
    edges: list[Edge]
    groups: list[list[int]]  # smooth regions (ADR 0008 §4): the unit for edits, callouts and comments
    extract_s: float
    units: str = "mm"
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "PartFacts":
        if d["schema_version"] != SCHEMA_VERSION:
            raise ValueError(f"PartFacts schema {d['schema_version']} != supported {SCHEMA_VERSION}")
        faces = [
            FaceFacts(**{**f, "centroid": tuple(f["centroid"]),
                         "normal": _tuple(f["normal"]), "axis": _tuple(f["axis"]),
                         "access": {k: Access(**a) for k, a in f["access"].items()}})
            for f in d["faces"]
        ]
        return cls(**{**d, "bbox": (tuple(d["bbox"][0]), tuple(d["bbox"][1])), "faces": faces,
                      "edges": [Edge(**e) for e in d["edges"]]})


def _tuple(v):
    return None if v is None else tuple(v)


def face_groups(faces: list[FaceFacts], edges: list[Edge]) -> list[list[int]]:
    """Union faces across tangent edges that join coplanar planes or touch a concave cylinder.

    A pocket's walls and corner fillets become one group, a floor split by a boolean becomes
    one group, and convex fillets stay separate so they don't merge the outside of the part.
    """
    by_id = {f.id: f for f in faces}
    parent = {f.id: f.id for f in faces}

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for e in edges:
        if e.kind != "tangent":
            continue
        fa, fb = by_id[e.a], by_id[e.b]
        if (fa.surface == fb.surface == "plane") or fa.concave_radius or fb.concave_radius:
            parent[root(e.a)] = root(e.b)
    groups: dict[int, list[int]] = {}
    for f in faces:
        groups.setdefault(root(f.id), []).append(f.id)
    return sorted(groups.values())
