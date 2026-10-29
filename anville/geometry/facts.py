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
    stock_neighbors: list[int] = field(default_factory=list)  # stock faces sharing an edge with this one


@dataclass(frozen=True)
class Edge:
    """An edge between two distinct faces."""

    a: int
    b: int
    kind: str                  # tangent | concave | convex
    axis: str | None = None    # sharp concave straight edges parallel to a principal axis
    radius: float | None = None  # set when an edit fillets that edge; None means sharp


@dataclass
class EdgeChamfer:
    """An exterior edge chamfer (RFC 0002 §1): planar, 45° ± 0.5° down and outward, bridging a stock
    side face and the stock bottom face along a straight principal edge."""

    face_id: int
    side_face: int
    bottom_face: int
    axis: str        # "X" | "Y": the chamfered edge's direction
    outward: str     # "+X" | "-X" | "+Y" | "-Y": the side the chamfer faces
    angle: float     # degrees between the face normal and -Z
    leg: float       # mm, measured across the edge: cross-section width / √2
    start: Vec       # the chamfer's bottom edge (on the stock bottom), one end
    end: Vec         # and the other


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
    edge_chamfers: list[EdgeChamfer] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def labelled_groups(self) -> dict[str, list[int]]:
        """Groups as the agent names them: G1, G2, ... in `groups` order (stable for one STEP file)."""
        return {f"G{i}": ids for i, ids in enumerate(self.groups, 1)}

    def group_faces(self, label: str) -> list[int]:
        try:
            return self.labelled_groups()[label]
        except KeyError:
            raise ValueError(f"unknown face group {label!r}; this part has G1..G{len(self.groups)}") from None

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
        chamfers = [EdgeChamfer(**{**c, "start": tuple(c["start"]), "end": tuple(c["end"])})
                    for c in d.get("edge_chamfers", [])]
        return cls(**{**d, "bbox": (tuple(d["bbox"][0]), tuple(d["bbox"][1])), "faces": faces,
                      "edges": [Edge(**e) for e in d["edges"]], "edge_chamfers": chamfers})


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
