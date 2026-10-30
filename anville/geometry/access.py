"""What assess_back_chamfer returns (RFC 0002 §2-3). Pure Python, no OCP: the assessment runs on the
geometry image, and the review job, which has no OCP, rebuilds it from its dict.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from anville.geometry.facts import Vec


@dataclass
class Check:
    name: str            # a-f, as in RFC 0002 §2
    passed: bool
    margin: float | None  # mm (degrees for a); positive = room to spare
    detail: str


@dataclass
class Relief:
    """A pocket to cut in one soft jaw, derived from the swept cutter plus the margin (for the setup sheet)."""

    jaw: str                # "start" | "end" of the chamfered edge
    origin: Vec             # mm, the pocket's minimum corner in part coordinates
    size: Vec               # mm along X, Y, Z
    contact_kept: float     # share of the jaw's grip on the part that the pocket leaves


@dataclass
class BackChamferAccess:
    face_id: int
    tool: str
    checks: list[Check]
    reliefs: list[Relief] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.passed for c in self.checks)

    def to_dict(self) -> dict:
        return {**asdict(self), "ok": self.ok}

    @classmethod
    def from_dict(cls, d: dict) -> "BackChamferAccess":
        """The inverse of to_dict (`ok` is derived): analyze() runs on the OCP image, the review elsewhere."""
        return cls(d["face_id"], d["tool"], [Check(**c) for c in d["checks"]],
                   [Relief(**{**r, "origin": tuple(r["origin"]), "size": tuple(r["size"])}) for r in d["reliefs"]])
