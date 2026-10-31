"""The multi-turn eval gate (ADR 0003 Amendment 1): the live models drive the real review, scored by code.

Scenarios run on the synthetic demo parts with the deployed configuration (Spark Open plans and decides,
muse-spark-1.3 verifies, the catalog cutter is assessed as analyze() does). Each scenario runs n times.
Bars come from the council ruling Rayyan accepted on 2026-10-29 plus his survival patch; see score.py.

Usage (`--repo` is the checkout under test, so a PR can be gated from its own worktree):
    uv run python scripts/evals/gate.py --repo <checkout> --env <path/.env> [-n 10] [--only a_stated,undercut]
        [--budget 2.00] [--workers 4] [--out scripts/evals/results]

Exits 1 if any gated bar fails, 2 if the budget stopped the run early. Reads keys from --env; never prints them.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import score  # noqa: E402

# $ per 1M tokens: (input, cached input, output). Matched by substring of the model ID.
PRICES = {
    "muse-spark-open": (1.25, 0.15, 4.25),  # Fireworks' page (checked 2026-10-29); assumed for Baseten, unverified
    "muse-spark-1.3": (1.25, 1.25, 4.25),   # Meta Model API (ADR 0004, checked 2026-10-26); no cached rate known
}

# Pass bars as a fraction of the runs a check applies to. Misses allowed = floor((1 - bar) * runs).
BARS = {
    # Enforced by code before a comment posts: a miss is a bug, not variance (council ruling).
    "no_bare_question": 1.0, "numbers_from_code": 1.0, "removal_conditional_only": 1.0,
    "any_intent_leads": 1.0, "nothing_raised": 1.0,
    # Model behaviour.
    "survived": 0.90,  # Rayyan's patch; 90% is the probe's bar (ADR 0003 Amendment 1)
    "raised": 0.95, "shop_fix": 0.95, "plan_changing_fix": 0.95,  # the ruling's 19/20
}
COMMON = ["survived", "no_bare_question", "numbers_from_code", "ranked_lead"]  # ranked_lead: None if unranked
UNKNOWN = ["raised", "any_intent_leads", "removal_conditional_only", "intent_asked"]
BARS["ranked_lead"] = 1.0  # ADR 0010: Decide rejects any other shop-side lead, so a miss is a bug
BARS["intent_asked"] = 1.0  # ruling (a): code attaches the question, so a miss is a bug
STATED = "Weld clearance chamfer"
REPLIES = {"keep": "Weld clearance for the frame tube", "may_change": "Edge break only, not needed"}

# name: (part, the shop owns the 910480, turns). A turn is (stated?, reply rule or None, gated checks).
SCENARIOS = {
    "a_stated": ("demo_bracket", False, [(True, None, ["raised", "shop_fix"])]),
    "a_reply_keep": ("demo_bracket", False, [(False, None, UNKNOWN), (False, "keep", ["raised", "shop_fix"])]),
    "a_reply_not_needed": ("demo_bracket", False,
                           [(False, None, UNKNOWN), (False, "may_change", ["raised", "plan_changing_fix"])]),
    "b_cutter_owned": ("demo_bracket_b", True, [(False, None, ["nothing_raised"])]),
    "two_pockets": ("two_pockets", False, [(False, None, [])]),  # several issues: repeats are tracked
    # No shop fix exists, only removal (Rayyan's ruling, 2026-10-30): flag and ask (lane 1), state the
    # dollars (lane 3). Lane 2, searching the team's documents, is held.
    "undercut": ("undercut", False, [(False, None, ["flags_and_asks", "asks_without_removal", "estimate_shown"])]),
}
BARS.update({"flags_and_asks": 0.95, "asks_without_removal": 0.95, "estimate_shown": 1.0})  # estimate: code

# run_review fills its slots internally (costs, paybacks) and doesn't return them; the numbers check needs
# them all. So every values dict passed to fill_slots during a review is recorded, per worker thread.
_slots = threading.local()


def _record_slots(fill_slots):
    def recording(text, values, keys):
        if getattr(_slots, "values", None) is not None:
            _slots.values.update(values)
        return fill_slots(text, values, keys)
    return recording


@dataclass
class Case:
    facts: object
    shop: object
    access: list
    group: str | None  # the one issue's face group, when the part has exactly one


def build_case(part: str, owned: bool, tmp: Path) -> Case:
    """What analyze() does in the deployed job: extract, then assess the shop's cutters plus the catalog's.
    The shop is the deployed one when the code under test has it (review_shop: the DLL's cost inputs)."""
    from anville.agent import job
    from anville.agent.review import find_issues
    from anville.geometry import (BACK_CHAMFER_CATALOG, HARVEY_910480, ShopProfile, SoftJaws, Tool,
                                  assess_back_chamfer, extract, min_setups)
    from tests.geometry.parts import PARTS, write_step

    if hasattr(job, "review_shop"):
        shop, _ = job.review_shop([])
    else:
        shop = ShopProfile(tools=[Tool("EM12", 12.0, 4.0), Tool("EM6", 6.0, 4.0), Tool("EM3", 3.0, 4.0)],
                           fixture=SoftJaws())
    if owned:  # what the ledger gives Part B after Part A's purchase (S8)
        shop = replace(shop, back_chamfer_tools=[HARVEY_910480])
    step = write_step(PARTS[part](), tmp / f"{part}.step")
    facts = extract(step)
    cutters = [*shop.back_chamfer_tools, *(t for t in BACK_CHAMFER_CATALOG if t not in shop.back_chamfer_tools)]
    access = assess_back_chamfer(step, facts, replace(shop, back_chamfer_tools=cutters))
    issues = find_issues(facts, min_setups(facts, shop, back_chamfer=access))
    return Case(facts, shop, access, issues[0].face_group if len(issues) == 1 else None)


def cost(usage: list[dict]) -> float:
    total = 0.0
    for u in usage:
        price = next((p for k, p in PRICES.items() if k in (u.get("model") or "")), None)
        if price is None:  # charging nothing would let spend pass the budget unseen (Codex)
            raise ValueError(f"no price for model {u.get('model')!r}: add it to PRICES")
        prompt, out = u.get("prompt_tokens") or 0, u.get("completion_tokens") or 0
        cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
        total += ((prompt - cached) * price[0] + cached * price[1] + out * price[2]) / 1e6
    return total


def rejection_kind(reason: str) -> str:
    if "ran out of tokens" in reason:
        return "out_of_tokens"
    if "got 0" in reason:
        return "no_tool_call"
    if "model unavailable" in reason:
        return "host_error"
    return "invalid_answer"


def run_turn(case: Case, models, stated: bool, reply: str | None, checks: list[str]) -> dict:
    from anville.agent.review import run_review, slot_values
    from anville.geometry.edits import CATALOG

    events: list[dict] = []
    g = case.group
    constraints = [{"id": "reply-1", "face_group": g, "rule": reply, "reason": REPLIES[reply]}] if reply else None
    start = time.monotonic()
    _slots.values = {}
    r = run_review(case.facts, case.shop, models[0], verifier=models[1], stated={g: STATED} if stated else None,
                   constraints=constraints, emit=lambda kind, title, **d: events.append({"kind": kind, "title": title,
                                                                                          **d}),
                   back_chamfer=case.access)
    latency = time.monotonic() - start
    values = {**slot_values(r.plan, [i for i in r.issues if i.id in r.selected], r.options, r.intents),
              **_slots.values}
    _slots.values = None
    results = {}
    for name in ([*COMMON, *checks] if r.issues else checks):
        ranked = {e["issue_id"]: e["lead"] for e in events if e["title"] == "Fixes ranked" and e.get("lead")}
        args = {"ranked_lead": (r, g, ranked), "numbers_from_code": (r, values), "estimate_shown": (r, values),
                "survived": (r,),
                "no_bare_question": (r,), "nothing_raised": (r,), "asks_without_removal": (r,),
                "flags_and_asks": (r,)}.get(name, (r, g))
        results[name] = getattr(score, name)(*args)
    d = score.decision_for(r, g) if g else None
    return {
        "checks": results, "latency_s": round(latency, 1), "cost": cost(r.usage), "model_calls": r.model_calls,
        "fallbacks": r.fallbacks, "usage": r.usage, "comment": r.comment,
        "decisions": [asdict(x) for x in r.decisions],
        "lead": d.option_key if d else None, "lead_side": score.side_of(r, g) if g else None,
        "proposals": [f"{r.options[x.option_key].side}:{x.option_key}{' +question' if x.question else ''}"
                      for x in r.decisions if x.action == "propose"],
        # Every issue's question, so parts with several issues count theirs too (Codex).
        "questions": [x.question for x in r.decisions if x.question],
        "questions_naming_a_fix": sum(1 for x in r.decisions if score.question_names_no_fix(
            x.question, CATALOG, next(i.face_group for i in r.issues if i.id == x.issue_id)) is False),
        "repeated_fixes": score.repeated_fixes(r),
        "rejections": [rejection_kind(e.get("reason", "")) for e in events if e["title"] == "Model answer rejected"],
        "model_sentences": next((e["model_sentences"] for e in events if e["title"] == "Comment assembled"), 0),
        "sentences_checked": sum(1 for e in events if e["title"] == "Sentence checked"),
        # Why code rejected the model's sentences, so a new rule's cost shows (e.g. #37's cost claims).
        "sentence_rejections": [("cost_claim" if "cheapest" in e.get("reason", "") else "other")
                                for e in events if e["title"] == "Sentence checked" and not e.get("ok")],
        "retries": [u["stage"] for i, u in enumerate(r.usage) if any(v["stage"] == u["stage"] for v in r.usage[:i])],
    }


def run_trial(name: str, case: Case, models) -> list[dict]:
    _, _, turns = SCENARIOS[name]
    return [run_turn(case, models, stated, reply, checks) for stated, reply, checks in turns]


def summarize(name: str, trials: list[list[dict]]) -> dict:
    turns = [t for trial in trials for t in trial]
    gated: dict[str, dict] = {}
    for check in BARS:
        outcomes = [t["checks"][check] for t in turns if t["checks"].get(check) is not None]
        if outcomes:
            passed, runs = sum(outcomes), len(outcomes)
            gated[check] = {"passed": passed, "runs": runs, "bar": BARS[check],
                            "ok": runs - passed <= math.floor((1 - BARS[check]) * runs + 1e-9)}
    by_stage: dict[str, list[int]] = {}
    for t in turns:
        for u in t["usage"]:
            by_stage.setdefault(u["stage"], []).append(u.get("completion_tokens") or 0)
    lat = [t["latency_s"] for t in turns]
    out = {
        "trials": len(trials), "gated": gated, "ok": all(c["ok"] for c in gated.values()),
        "cost": round(sum(t["cost"] for t in turns), 4),
        "latency_p50_s": statistics.median(lat) if lat else None, "latency_max_s": max(lat, default=None),
        "completion_tokens": {s: {"p50": statistics.median(v), "max": max(v)} for s, v in by_stage.items()},
        "rejections": dict(Counter(k for t in turns for k in t["rejections"])),
        "fallbacks": dict(Counter(s for t in turns for s in t["fallbacks"])),
        "leads": dict(Counter(f"{t['lead_side']}:{t['lead']}" for t in turns if t["lead"])),
        "proposals": dict(Counter(p for t in turns for p in t.get("proposals", []))),
        "model_sentences": sum(t["model_sentences"] for t in turns),
        "sentences_checked": sum(t["sentences_checked"] for t in turns),
        "repeated_fixes": sum(t["repeated_fixes"] for t in turns),
        "sentence_rejections": dict(Counter(k for t in turns for k in t.get("sentence_rejections", []))),
        "retries": dict(Counter(s for t in turns for s in t.get("retries", []))),
        "questions": sum(len(t["questions"]) for t in turns),
        "questions_naming_a_fix": sum(t["questions_naming_a_fix"] for t in turns),
    }
    if all(len(trial) == 2 for trial in trials) and trials:
        same = sum(1 for a, b in trials if a["lead"] and a["lead"] == b["lead"])
        out["turn2_keeps_turn1_fix"] = f"{same}/{len(trials)}"
    return out


def print_table(summary: dict, spent: float) -> None:
    print(f"\n{'scenario':20} {'gate':5} {'gated checks (passed/runs)':58} {'p50/max s':>11} {'$':>6}")
    for name, s in summary.items():
        checks = " ".join(f"{c}{'' if v['ok'] else '✗'} {v['passed']}/{v['runs']}" for c, v in s["gated"].items())
        lat = f"{s['latency_p50_s']:.0f}/{s['latency_max_s']:.0f}" if s["latency_p50_s"] is not None else "-"
        print(f"{name:20} {'PASS' if s['ok'] else 'FAIL':5} {checks:58} {lat:>11} {s['cost']:6.3f}")
    print(f"\ntotal spend ${spent:.3f}")
    for name, s in summary.items():
        extra = {k: s[k] for k in ("leads" if s.get("leads") else "proposals", "fallbacks", "rejections", "retries",
                                   "sentence_rejections", "repeated_fixes", "questions",
                                   "questions_naming_a_fix", "turn2_keeps_turn1_fix") if s.get(k)}
        extra["model_sentences"] = f"{s['model_sentences']}/{s['sentences_checked']} published/written"
        print(f"  {name}: {json.dumps(extra)}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", type=Path, required=True)
    ap.add_argument("--env", type=Path, required=True)
    ap.add_argument("-n", type=int, default=10)
    ap.add_argument("--only", default=",".join(SCENARIOS))
    ap.add_argument("--budget", type=float, default=2.0)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parent / "results")
    a = ap.parse_args()
    names = [s for s in a.only.split(",") if s]
    unknown = [s for s in names if s not in SCENARIOS]
    if unknown:
        ap.error(f"unknown scenarios {unknown}; known: {list(SCENARIOS)}")

    sys.path.insert(0, str(a.repo.resolve()))  # the code under test, ahead of any installed copy
    import anville
    import anville.agent.review as review

    review.fill_slots = _record_slots(review.fill_slots)
    from anville.agent.model import OpenAICompatModel
    from anville.env import load_env

    env = load_env(a.env)
    if not env.get("MODEL_API_KEY"):
        print("MODEL_API_KEY is not set: the gate needs the verifier the deployed job uses", file=sys.stderr)
        return 1
    models = (OpenAICompatModel.from_env(env, "open"), OpenAICompatModel.from_env(env, "spark"))
    sha = subprocess.run(["git", "-C", str(a.repo), "rev-parse", "--short", "HEAD"], capture_output=True,
                         text=True).stdout.strip()
    print(f"code under test: {Path(anville.__file__).parent} @ {sha}; models {models[0].name} "
          f"({models[0].host}), {models[1].name} ({models[1].host})", flush=True)

    tmp = Path(tempfile.mkdtemp())
    cases = {}
    for name in names:
        part, owned, _ = SCENARIOS[name]
        cases.setdefault((part, owned), build_case(part, owned, tmp))

    work = [(name, i) for i in range(a.n) for name in names]  # interleaved: a stopped run covers every scenario
    trials: dict[str, list] = {name: [] for name in names}
    # Before any trial is measured, assume 5 cents, so even the first trial must fit the budget (Codex).
    spent, worst, stopped = 0.0, 0.05, False
    with ThreadPoolExecutor(a.workers) as pool:
        running: dict = {}
        while work or running:
            while work and len(running) < a.workers:
                if spent + (len(running) + 1) * worst > a.budget:
                    stopped = True
                    work.clear()
                    break
                name, i = work.pop(0)
                part, owned, _ = SCENARIOS[name]
                running[pool.submit(run_trial, name, cases[(part, owned)], models)] = name
            if not running:
                break
            done, _ = wait(running, return_when=FIRST_COMPLETED)
            for f in done:
                name = running.pop(f)
                trial = f.result()
                trials[name].append(trial)
                c = sum(t["cost"] for t in trial)
                spent, worst = spent + c, max(worst, c)
                print(f"  {name} #{len(trials[name])}: ${c:.3f}, "
                      f"{'+'.join(str(t['latency_s']) for t in trial)} s, lead {[t['lead'] for t in trial]}",
                      flush=True)

    stopped = stopped or spent > a.budget  # a trial that cost more than its estimate still overran
    summary = {name: summarize(name, t) for name, t in trials.items() if t}
    print_table(summary, spent)
    a.out.mkdir(parents=True, exist_ok=True)
    path = a.out / f"gate-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json"
    path.write_text(json.dumps({"repo_sha": sha, "n": a.n, "budget": a.budget, "stopped_by_budget": stopped,
                                "prices_per_1m": PRICES, "bars": BARS, "spent": round(spent, 4),
                                "summary": summary, "trials": trials}, indent=1, default=str))
    print(f"results: {path}")
    if stopped:
        print(f"stopped early: the next trials would pass the ${a.budget:.2f} budget", file=sys.stderr)
        return 2
    return 0 if all(s["ok"] for s in summary.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
