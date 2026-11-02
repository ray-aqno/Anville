"""The local connector (ADR 0001: LocalSource and LocalSink): the production loop on a folder of STEP files.

Review, reply and shop note run the same job.py calls modal_app makes, with scripted models; only where the
review comes from and where it goes differ. Part A is demo_bracket, Part B demo_insert (the learning beat).
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from anville.agent.job import review_shop, setup_count_message
from anville.agent.review import find_issues
from anville.connectors.local import LocalSink, LocalSource, loop
from anville.geometry import extract, min_setups
from tests.agent.test_reply import keep, rules
from tests.agent.test_review import BUY, Scripted, decide, plan, propose_route, requests_for

OFF = {"ANVILLE_AGENT": "off"}  # code's review: no model, no key


@pytest.fixture
def folder(tmp_path):
    from tests.geometry.parts import PARTS, write_step  # OCP

    for name in ("demo_bracket", "demo_insert"):
        write_step(PARTS[name](), tmp_path / f"{name}.step")
    return tmp_path


def chamfer_of(step):
    f = extract(step)
    (issue,) = find_issues(f, min_setups(f, review_shop([])[0]))
    return issue.face_group


def buys(group):
    """The scripted review that decides to buy the catalog back-chamfer cutter (the demo's Part A decision)."""
    return Scripted(plan(*requests_for(group)),
                    decide({"issue_id": "I1", "action": "propose", "option_key": BUY, "reason": "x"}))


def trace_of(folder, name):
    return [json.loads(line) for line in (folder / f"{name}.trace.jsonl").read_text().splitlines()]


def test_a_review_writes_the_comment_the_sheet_and_the_trace(folder):
    src, sink = LocalSource(folder), LocalSink(folder)
    loop.review(src.part("demo_bracket.step"), sink, OFF)
    review = (folder / "demo_bracket.review.md").read_text()
    assert "2 setup(s) on a 3-axis mill" in review                       # code's review, from the SetupPlan
    assert review.startswith("# Anville reviews: demo_bracket.step\n")
    sheet = json.loads((folder / "demo_bracket.sheet.json").read_text())
    assert len(sheet["setups"]) == 2 and sheet["version_id"] == src.part("demo_bracket.step").version_id
    assert f"Setup sheet for the shop: demo_bracket.sheet-{sheet['sheet_id'][:8]}.json" in review
    events = trace_of(folder, "demo_bracket")
    assert [e["title"] for e in events if e["kind"] in ("run_started", "verify", "comment_posted", "run_finished")] \
        == ["Reviewing a new version", "Setup count computed from geometry", "Posted the review", "Review finished"]
    ready = next(e for e in events if e["title"] == "Setup sheet ready")
    assert ready["detail"]["sheet_id"] == sheet["sheet_id"] and ready["detail"]["setups"] == 2
    # The panel's ADR 0007 event shape, cursors increasing, one run
    assert all(set(e) == {"cursor", "run_id", "seq", "ts", "document_id", "version_id", "element_id", "kind",
                          "title", "detail", "face_ids"} for e in events)
    assert [e["cursor"] for e in events] == sorted({e["cursor"] for e in events})
    assert {e["run_id"] for e in events} == {f"{sheet['version_id'][:8]}-1"}


def test_the_models_review_carries_its_credits_and_the_sheet_buys_its_cutter(folder):
    part = LocalSource(folder).part("demo_bracket.step")
    loop.review(part, LocalSink(folder), {}, model=buys(chamfer_of(part.step)))
    review = (folder / "demo_bracket.review.md").read_text()
    assert "model call" in review.splitlines()[-1] or any("model call" in line for line in review.splitlines())
    sheet = json.loads((folder / "demo_bracket.sheet.json").read_text())
    rows = [t for s in sheet["setups"] for t in s["tools"]]
    assert len(sheet["setups"]) == 1 and [t["owned"] for t in rows if t["name"] == "910480"] == [False]


def test_a_reply_becomes_a_constraint_and_is_answered_in_the_thread(folder):
    src, sink = LocalSource(folder), LocalSink(folder)
    part = src.part("demo_bracket.step")
    g = chamfer_of(part.step)
    loop.review(part, sink, OFF)
    reply = f"Keep {g}, it's the weld clearance for the frame tube."
    replan = Scripted(plan(*requests_for(g)), decide(propose_route()))
    loop.reply(part, sink, {}, reply, reader=Scripted(rules(keep(group=g))), model=replan)
    thread = (folder / "demo_bracket.review.md").read_text()
    assert f"> {reply}" in thread and thread.index("Designer") < thread.rindex("Anville")
    (row,) = sink.store.read_ledger("constraint", f"doc:{part.document_id}")
    assert row["payload"]["face_group"] == g and row["payload"]["rule"] == "keep"
    runs = [e["run_id"] for e in trace_of(folder, "demo_bracket")]
    assert runs[-1] == f"{part.version_id[:8]}-r1"                       # the panel's re-plan run, same version
    assert "Constraint recorded" in [e["title"] for e in trace_of(folder, "demo_bracket")]


def test_a_reply_before_any_review_is_refused(folder):
    part = LocalSource(folder).part("demo_bracket.step")
    with pytest.raises(loop.LocalError, match="review it first"):
        loop.reply(part, LocalSink(folder), OFF, "Keep it.")


def test_the_learning_beat_runs_offline(folder):
    """Part A's review buys the 910480, the machinist ticks it arrived, and Part B is 1 setup, saying why."""
    src, sink = LocalSource(folder), LocalSink(folder)
    a = src.part("demo_bracket.step")
    loop.review(a, sink, {}, model=buys(chamfer_of(a.step)))
    loop.shop_note(a, sink, arrived=["910480"])
    loop.review(src.part("demo_insert.step"), sink, {}, model=Scripted())
    review = (folder / "demo_insert.review.md").read_text()
    assert "1 setup" in review and "the 910480 the shop bought" in review
    assert "Used what the shop learned" in [e["title"] for e in trace_of(folder, "demo_insert")]


def test_a_shop_note_for_a_cutter_the_sheet_doesnt_buy_is_refused(folder):
    a = LocalSource(folder).part("demo_bracket.step")
    loop.review(a, LocalSink(folder), OFF)                                # code's review buys nothing
    with pytest.raises(ValueError, match="already owns|not on this sheet"):
        loop.shop_note(a, LocalSink(folder), arrived=["910480"])


def test_each_shop_learns_on_its_own(folder):
    src = LocalSource(folder)
    a = src.part("demo_bracket.step")
    loop.review(a, LocalSink(folder), {}, model=buys(chamfer_of(a.step)))
    loop.shop_note(a, LocalSink(folder), arrived=["910480"])
    loop.review(src.part("demo_insert.step"), LocalSink(folder, shop="take2"), OFF)
    assert "2 setup(s)" in (folder / "demo_insert.review.md").read_text()


def test_the_written_fix_stands_in_for_the_branch(folder):
    """No CAD edit API here: the removal alternative is the intent as a written line (ADR 0001)."""
    loop.review(LocalSource(folder).part("demo_bracket.step"), LocalSink(folder), OFF)
    (line,) = [x for x in (folder / "demo_bracket.review.md").read_text().splitlines() if x.startswith("Written fix")]
    assert "would take it to 1 setup(s)" in line and "no CAD edit here" in line


def test_a_changed_file_is_a_new_version_and_a_rerun_a_new_run(folder):
    src, sink = LocalSource(folder), LocalSink(folder)
    v1 = src.part("demo_bracket.step")
    loop.review(v1, sink, OFF)
    loop.review(v1, sink, OFF)
    assert {e["run_id"] for e in trace_of(folder, "demo_bracket")} == {f"{v1.version_id[:8]}-1",
                                                                         f"{v1.version_id[:8]}-2"}
    (folder / "demo_bracket.step").write_bytes((folder / "demo_insert.step").read_bytes())
    assert src.part("demo_bracket.step").version_id != v1.version_id
    assert src.part("demo_bracket.step").document_id == v1.document_id


def test_a_part_outside_the_folder_is_refused(folder, tmp_path_factory):
    other = tmp_path_factory.mktemp("elsewhere") / "x.step"
    other.write_bytes(b"")
    with pytest.raises(loop.LocalError, match="not in"):
        LocalSource(folder).part(str(other))


def test_the_setup_count_message_is_production_s():
    """modal_app needs Modal, which tests don't install: its _review_message is read from the source and run."""
    import ast

    src = Path("anville/modal_app.py").read_text()
    fn = next(n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == "_review_message")
    scope: dict = {}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "modal_app.py", "exec"), scope)
    for plan_ in ({"count": 2, "setups": [{"directions": ["+Z"]}, {"directions": ["-Z", "+X"]}], "unreachable": []},
                  {"count": 1, "setups": [], "unreachable": [{"reason": "no_tool"}, {"reason": "no_direction"}]}):
        assert setup_count_message("abcdef0123", plan_) == scope["_review_message"]("abcdef0123", plan_)


def test_the_cli_reviews_a_part(folder):
    out = subprocess.run([sys.executable, "scripts/anville_local.py", "review", str(folder / "demo_bracket.step")],
                         capture_output=True, text=True, env={"PATH": "", "ANVILLE_AGENT": "off",
                                                              "PYTHONPATH": str(Path.cwd())}, timeout=240)
    assert out.returncode == 0, out.stderr
    assert "demo_bracket.review.md" in out.stdout and (folder / "demo_bracket.trace.jsonl").exists()


# --- Codex P2s on the first draft --------------------------------------------------------------------------------

def test_a_second_review_keeps_the_thread_it_follows(folder):
    """Codex P2: a rerun replaced review.md, replies and answers included. On Onshape the old comment stays."""
    src, sink = LocalSource(folder), LocalSink(folder)
    part = src.part("demo_bracket.step")
    loop.review(part, sink, OFF)
    loop.reply(part, sink, OFF, "Keep it, it's the weld clearance.")
    loop.review(part, sink, OFF)
    thread = (folder / "demo_bracket.review.md").read_text()
    assert "> Keep it, it's the weld clearance." in thread
    assert thread.count(f"## Review of version {part.version_id[:8]}") == 2
    assert thread.index("### Designer") < thread.rindex("## Review of version")   # the reply sits under review 1


def test_a_shop_note_counts_only_for_the_shop_its_sheet_was_made_for(folder):
    """Codex P2: a sheet made for one shop, noted under another, would teach the wrong shop."""
    a = LocalSource(folder).part("demo_bracket.step")
    loop.review(a, LocalSink(folder), {}, model=buys(chamfer_of(a.step)))
    with pytest.raises(loop.LocalError, match="made for shop"):
        loop.shop_note(a, LocalSink(folder, shop="take2"), arrived=["910480"])
    assert LocalSink(folder, shop="take2").shop_ledger() == [] and LocalSink(folder).shop_ledger() == []


def test_the_cli_takes_shop_after_the_command(folder, capsys):
    """Codex P2: the documented `review <part> --shop NAME` was an argparse error."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("anville_local", "scripts/anville_local.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    for argv in (["review", str(folder / "missing.step"), "--shop", "take2"],
                 ["--shop", "take2", "shop-note", str(folder / "demo_bracket.step"), "--arrived", "910480"]):
        assert cli.main(argv) == 2                                       # refused by the connector, not argparse
    err = capsys.readouterr().err
    assert "no STEP file" in err and "no setup sheet yet" in err and "unrecognized" not in err


def test_the_run_shows_as_started_before_the_slow_analysis(folder, monkeypatch):
    """Codex P2: run_started sat in the buffer through STEP extraction, so a replay showed no run in progress."""
    real, seen = loop.analyze_step, []

    def analyze(step, ledger=()):
        seen.append([e["title"] for e in trace_of(folder, "demo_bracket")])
        return real(step, ledger)

    monkeypatch.setattr(loop, "analyze_step", analyze)
    loop.review(LocalSource(folder).part("demo_bracket.step"), LocalSink(folder), OFF)
    assert seen == [["Reviewing a new version"]]


def test_an_earlier_review_s_sheet_link_still_opens_its_sheet(folder):
    """Codex P2: a rerun overwrote the sheet the earlier review links to."""
    src, sink = LocalSource(folder), LocalSink(folder)
    a = src.part("demo_bracket.step")
    loop.review(a, sink, {}, model=buys(chamfer_of(a.step)))   # buys the 910480: 1 setup
    loop.review(a, sink, OFF)                                   # buys nothing: 2 setups
    links = [x.split(": ", 1)[1] for x in (folder / "demo_bracket.review.md").read_text().splitlines()
             if x.startswith("Setup sheet for the shop: ")]
    assert [len(json.loads((folder / name).read_text())["setups"]) for name in links] == [1, 2]
    assert len(json.loads((folder / "demo_bracket.sheet.json").read_text())["setups"]) == 2  # the latest


def test_every_comment_anville_writes_has_its_own_id_and_knows_it_wrote_it(folder):
    """RFC 0006 P1: the self-loop guard is per comment, so the sink returns an id for each comment it writes."""
    src, sink = LocalSource(folder), LocalSink(folder)
    part = src.part("demo_bracket.step")
    g = chamfer_of(part.step)
    loop.review(part, sink, OFF)
    loop.reply(part, sink, {}, f"Keep {g}, it's the weld clearance for the frame tube.",
               reader=Scripted(rules(keep(group=g))), model=Scripted(plan(*requests_for(g)), decide(propose_route())))
    posted = [e for e in trace_of(folder, "demo_bracket") if e["kind"] == "comment_posted"]
    ids = [e["detail"]["comment_id"] for e in posted]
    assert len(ids) == 2 and len(set(ids)) == 2
    thread = (folder / "demo_bracket.review.md").read_text()
    assert all(f'<a id="{i}"></a>' in thread for i in ids)
    assert all(sink.is_own(i) for i in ids)
    designer = next(e for e in trace_of(folder, "demo_bracket") if e["kind"] == "run_started"
                    and e["run_id"].endswith("-r1"))["detail"]["comment_id"]
    assert not sink.is_own(designer) and f'<a id="{designer}"></a>' in thread


def test_two_files_with_the_same_bytes_are_two_documents_with_their_own_versions(folder):
    """Codex P1: a copied part shared the version id, so its review, sheet and comment record collided."""
    (folder / "copy.step").write_bytes((folder / "demo_bracket.step").read_bytes())
    src, sink = LocalSource(folder), LocalSink(folder)
    a, b = src.part("demo_bracket.step"), src.part("copy.step")
    assert a.version_id != b.version_id
    loop.review(a, sink, OFF)
    loop.review(b, sink, OFF)
    sa, sb = (json.loads((folder / f"{n}.sheet.json").read_text()) for n in ("demo_bracket", "copy"))
    assert sa["sheet_id"] != sb["sheet_id"]
    assert sink.store.event(f"sheet:{sb['sheet_id']}").document_id == b.document_id


def test_a_version_is_the_bytes_it_was_named_for_even_if_the_file_is_saved_again(folder):
    """Codex P2: the file can change between hashing and analysis; an Onshape version can't. The source keeps the
    version's bytes, as production keeps /data/step/<vid>.step."""
    src = LocalSource(folder)
    part = src.part("demo_bracket.step")
    before = (folder / "demo_bracket.step").read_bytes()
    (folder / "demo_bracket.step").write_bytes((folder / "demo_insert.step").read_bytes())
    assert part.step.read_bytes() == before and part.step != folder / "demo_bracket.step"
    assert src.part("demo_bracket.step").version_id != part.version_id


def cli_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("anville_local", "scripts/anville_local.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    return cli


def test_the_cli_says_which_cutter_the_shop_now_owns(folder, capsys):
    """Codex P1: the name was read from the wrong place, so a recorded note ended in a crash."""
    a = LocalSource(folder).part("demo_bracket.step")
    loop.review(a, LocalSink(folder), {}, model=buys(chamfer_of(a.step)))
    assert cli_module().main(["shop-note", str(folder / "demo_bracket.step"), "--arrived", "910480"]) == 0
    assert "The shop now owns 910480" in capsys.readouterr().out


def test_a_reply_answers_the_last_review_that_was_posted(folder, monkeypatch):
    """Codex P2: a review that failed before posting was taken as the one the reply answers."""
    src, sink = LocalSource(folder), LocalSink(folder)
    part = src.part("demo_bracket.step")

    def broken(step, ledger=()):
        raise RuntimeError("extract failed")

    with monkeypatch.context() as m:
        m.setattr(loop, "analyze_step", broken)
        with pytest.raises(RuntimeError):
            loop.review(part, sink, OFF)
    with pytest.raises(loop.LocalError, match="review it first"):
        loop.reply(part, sink, OFF, "Keep it.")
    loop.review(part, sink, OFF)                                          # run 2 posts
    with monkeypatch.context() as m:
        m.setattr(loop, "analyze_step", broken)
        with pytest.raises(RuntimeError):
            loop.review(part, sink, OFF)                                  # run 3 fails
    assert sink.last_review(part).key.endswith("#2")


def test_a_shop_note_can_answer_an_earlier_review_s_sheet(folder):
    """Codex P2: only the latest sheet could be answered, though each review links its own."""
    src, sink = LocalSource(folder), LocalSink(folder)
    a = src.part("demo_bracket.step")
    loop.review(a, sink, {}, model=buys(chamfer_of(a.step)))             # sheet 1 buys the 910480
    first = json.loads((folder / "demo_bracket.sheet.json").read_text())["sheet_id"]
    loop.review(a, sink, OFF)                                             # sheet 2, the latest, buys nothing
    loop.shop_note(a, sink, arrived=["910480"], sheet=first[:8])
    assert [r["payload"]["kind"] for r in sink.shop_ledger()] == ["shop_note", "tool_acquired"]
    with pytest.raises(loop.LocalError, match="no setup sheet"):
        loop.shop_note(a, sink, sheet="00000000")
