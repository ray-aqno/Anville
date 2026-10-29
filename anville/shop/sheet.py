"""setup_sheet: the plan as the shop runs it (the "traveler"), as JSON for the panel and the review comment.

One entry per setup: how the part sits, how it is held (relieved soft jaws where a back-chamfer cutter
needs them, RFC 0002 §3), which tools cut which faces, and the notes the machinist needs. Everything on
it comes from the plan, the shop profile and the back-chamfer checks; it is `predicted` until the shop
runs it. Face groups are the agent's G-labels, so the panel can tie a sheet line to a review comment.
"""

from anville.geometry.facts import PartFacts
from anville.geometry.plan import Setup, SetupPlan
from anville.geometry.shop import ShopProfile

SCHEMA = "anville.setup_sheet/1"
ORIENTATION = {
    "+Z": "top up, as modelled", "-Z": "flipped: bottom up",
    "+X": "on its side: +X face up", "-X": "on its side: -X face up",
    "+Y": "on its side: +Y face up", "-Y": "on its side: -Y face up",
}
WORKHOLDING_STATUS = "stated in the shop profile; not confirmed by the shop's machinist (RFC 0002 §3)"


def setup_sheet(facts: PartFacts, shop: ShopProfile, plan: SetupPlan, *, back_chamfer=(),
                provenance: dict[str, dict] | None = None) -> dict:
    """The setup sheet for `plan` in `shop`.

    `back_chamfer`: the `assess_back_chamfer` results the plan was made with; their reliefs go on the
    setups that back-chamfer. `provenance`: tool name -> where the shop got it (`apply_ledger`).
    """
    group = {fid: label for label, ids in facts.labelled_groups().items() for fid in ids}
    lo, hi = facts.bbox
    return {
        "schema": SCHEMA,
        "part": {"source_sha256": facts.source_sha256, "stock_mm": [round(b - a, 3) for a, b in zip(lo, hi)]},
        "shop": getattr(shop, "name", None),
        "axes": plan.axes,
        "setups": [_setup(n, s, facts, shop, group, back_chamfer, provenance or {})
                   for n, s in enumerate(plan.setups, 1)],
        "unreachable": [{"face_id": u.face_id, "group": group.get(u.face_id), "reason": u.reason, "detail": u.detail}
                        for u in plan.unreachable],
        "notes": ["Predicted from the CAD model's measured faces; not yet run on the machine."],
        "basis": "predicted",
    }


def _setup(n: int, setup: Setup, facts: PartFacts, shop: ShopProfile, group: dict, back_chamfer,
           provenance: dict) -> dict:
    cutters = {t.name: t for t in shop.back_chamfer_tools}
    chamfered = [a for a in setup.faces if a.tool in cutters]
    dirs = setup.directions
    notes = []
    if chamfered:
        notes += ["Cut the jaw reliefs before loading the part: pockets below, in part coordinates.",
                  "Machinist: verify the back-chamfer toolpath at the machine. The clearances are nominal, "
                  "from catalog sizes (RFC 0002 §2).",
                  "If the part is held any other way, the chamfer can't be finished from the top: flip it instead."]
    return {
        "number": n,
        "directions": list(dirs),
        "orientation": ORIENTATION[dirs[0]] if len(dirs) == 1 else f"one fixturing, indexed to {', '.join(dirs)}",
        "workholding": _workholding(shop, chamfered, cutters, facts, back_chamfer),
        "tools": _tools(setup, shop, cutters, group, provenance),
        "notes": notes,
    }


def _tools(setup: Setup, shop: ShopProfile, cutters: dict, group: dict, provenance: dict) -> list[dict]:
    mills = {t.name: t for t in shop.tools}
    faces: dict[str, list[int]] = {}
    for a in setup.faces:
        faces.setdefault(a.tool, []).append(a.face_id)
    out = []
    for name, ids in faces.items():
        if name in cutters:
            c = cutters[name]
            row = {"name": name, "type": "back_chamfer", "angle_deg": c.angle, "head_diameter_mm": c.head_diameter,
                   "neck_diameter_mm": c.neck_diameter, "neck_length_mm": c.neck_length,
                   "length_of_cut_mm": c.length_of_cut}
        elif name in mills:
            t = mills[name]
            row = {"name": name, "type": "endmill", "diameter_mm": t.diameter, "reach_mm": round(t.reach, 3)}
        else:
            raise ValueError(f"the plan uses {name!r}, which this shop profile doesn't have")
        row["faces"] = sorted(ids)
        row["groups"] = sorted({group[fid] for fid in ids if fid in group}, key=lambda g: int(g[1:]))
        if name in provenance:
            row["acquired"] = provenance[name]
        out.append(row)
    return out


def _workholding(shop: ShopProfile, chamfered: list, cutters: dict, facts: PartFacts, back_chamfer) -> dict:
    jaws = shop.fixture
    if jaws is None:
        return {"type": "not_stated", "status": "the shop profile states no workholding", "relieved": False,
                "grip_faces": None, "reliefs": [], "parallels": None}
    axis = {c.face_id: c.axis for c in facts.edge_chamfers}
    on_cutter = {(a.face_id, a.tool) for a in chamfered}
    passed = {(acc.face_id, acc.tool) for acc in back_chamfer if acc.ok}
    for fid, tool in sorted(on_cutter - passed):
        # Without its passing checks there are no reliefs to cut: refuse rather than send an incomplete sheet.
        raise ValueError(f"face {fid} is cut with back-chamfer cutter {tool!r} but has no passing assessment: "
                         "pass the assess_back_chamfer results the plan was made with")
    reliefs, seen, grip = [], set(), None
    for acc in back_chamfer:
        if (acc.face_id, acc.tool) not in on_cutter or not acc.ok:
            continue
        ax = axis[acc.face_id]
        grip = [f"-{ax}", f"+{ax}"]  # the jaws grip across the chamfered edge (RFC 0002 §3)
        for r in acc.reliefs:
            jaw = f"{'-' if r.jaw == 'start' else '+'}{ax} jaw"
            if (jaw, r.origin, r.size) in seen:
                continue  # every chamfer along one axis carries the same jaw's reliefs
            seen.add((jaw, r.origin, r.size))
            reliefs.append({"jaw": jaw, "origin_mm": list(r.origin), "size_mm": list(r.size),
                            "grip_kept": r.contact_kept})
    parallels = {"height_mm": jaws.parallel_height, "width_mm": jaws.parallel_width}
    if chamfered:
        head = max(cutters[a.tool].head_diameter for a in chamfered)
        setback = head / 2 + 1.0 if jaws.parallel_setback is None else jaws.parallel_setback
        parallels.update(setback_mm=round(setback, 3), from_side="chamfered")
    return {"type": "vise_soft_jaws", "vise_in": round(jaws.jaw_width / 25.4, 2), "jaw_material": "aluminum",
            "relieved": jaws.relieved and bool(reliefs), "grip_faces": grip, "reliefs": reliefs,
            "parallels": parallels, "status": WORKHOLDING_STATUS}
