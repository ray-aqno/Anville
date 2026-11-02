"""The local connector (ADR 0001): LocalSource reads a folder of .step files; LocalSink writes each review next to
its part, as files a person reads and a panel replays.

    <part>.review.md     the comment threads: each review, then each designer reply and Anville's answer
    <part>.sheet.json    the latest setup sheet the shop runs (job.sheet_for); each review links its own copy,
                         <part>.sheet-<id>.json, so an earlier review's link still opens the sheet it gave
    <part>.trace.jsonl   the run's ADR 0007 trace events, the ones the Onshape panel reads
    .anville/store.sqlite  production's store: the constraint and shop ledgers, review events, the trace

A part's document is its file name; its version is a hash of its bytes, so saving a changed file is a new version
and the designer's constraints carry over to it, as they do across Onshape versions.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from pathlib import Path

from anville.connectors import Part
from anville.store import Event, Store


class LocalError(Exception):
    """A request the local connector can't serve; the message says what to do instead."""


class LocalSource:
    """CadSource: a folder of .step files."""

    def __init__(self, folder: str | Path):
        self.folder = Path(folder).resolve()

    def part(self, ref: str) -> Part:
        path = (self.folder / ref).resolve()
        if path.parent != self.folder:
            raise LocalError(f"{ref} is not in {self.folder}: a source reviews only its own folder")
        if not path.is_file():
            raise LocalError(f"no STEP file at {path}")
        data = path.read_bytes()
        # A version belongs to its document, as on Onshape: a copied part is a second document (Codex P1). 24 hex
        # characters, like Onshape's ids.
        version = hashlib.sha256(path.stem.encode() + b"\0" + data).hexdigest()[:24]
        # The version's own bytes, kept as production keeps /data/step/<vid>.step: a save during the run can't
        # change what this version's review analyzes (Codex P2).
        kept = self.folder / ".anville" / "step" / f"{version}.step"
        if not kept.is_file():
            kept.parent.mkdir(parents=True, exist_ok=True)
            kept.write_bytes(data)
        return Part(name=path.stem, document_id=f"local:{path.stem}", version_id=version, step=kept)


class LocalSink:
    """ReviewSink: files next to the part, and production's Store in `<folder>/.anville/`. `shop` names the shop
    whose ledger the run reads and writes (job.shop_scope): every shop starts as the demo's and learns on its own."""

    def __init__(self, folder: str | Path, *, shop: str = "dll"):
        from anville.agent.job import shop_scope

        self.folder = Path(folder).resolve()
        (self.folder / ".anville").mkdir(exist_ok=True)
        self.store = Store(self.folder / ".anville" / "store.sqlite")
        self.shop_scope = shop_scope(None if shop == "dll" else shop)

    # --- ReviewSink ------------------------------------------------------------------------------------------------

    def trace(self, part: Part, run_id: str) -> "_Trace":
        return _Trace(self, part, run_id)

    def post_review(self, part: Part, message: str) -> str:
        """A review starts a new thread under the earlier ones, which stay, as comments do in Onshape (Codex P2).
        Returns its comment id, an anchor in the file."""
        out, cid = self._file(part, "review.md"), _comment_id()
        head = "" if out.is_file() else f"# Anville reviews: {part.name}.step\n"
        with out.open("a") as f:
            f.write(f'{head}\n## Review of version {part.version_id[:8]} <a id="{cid}"></a>\n\n{message}\n')
        return cid

    def post_reply(self, part: Part, reply_id: str, reply: str, message: str | None) -> str | None:
        """The designer's reply (`reply_id`: its comment id) joins the thread; Anville's answer follows it when
        there is one. Returns the answer's comment id."""
        out, cid = self._file(part, "review.md"), _comment_id() if message else None
        quoted = "\n".join(f"> {line}" for line in reply.splitlines() or [""])
        answer = f'\n### Anville <a id="{cid}"></a>\n\n{message}\n' if message else ""
        with out.open("a") as f:
            f.write(f'\n### Designer <a id="{reply_id}"></a>\n\n{quoted}\n{answer}')
        return cid

    def is_own(self, comment_id: str) -> bool:
        """Whether Anville wrote this comment: the store's record of what it posted, as on Onshape."""
        return self.store.post_for_comment(comment_id) is not None

    def store_sheet(self, part: Part, sheet_id: str, sheet: dict) -> str:
        record = {**sheet, "version_id": part.version_id, "sheet_id": sheet_id, "shop": self.shop_scope}
        self.store.record(f"sheet:{sheet_id}", part.document_id, record, settled=True)
        text = json.dumps(record, indent=2) + "\n"
        self._file(part, "sheet.json").write_text(text)          # the latest, which a shop note answers
        own = self._file(part, f"sheet-{sheet_id[:8]}.json")      # this review's, which its link keeps opening
        own.write_text(text)
        return own.name

    # --- what the loop reads back ----------------------------------------------------------------------------------

    def shop_ledger(self) -> list[dict]:
        return self.store.read_ledger("shop_outcome", self.shop_scope)

    def read_sheet(self, part: Part, sheet: str | None = None) -> dict:
        """The latest sheet, or the one a review linked (`sheet`: its id, or the 8 characters in its file name)."""
        path = self._file(part, f"sheet-{sheet[:8]}.json" if sheet else "sheet.json")
        if not path.is_file():
            raise LocalError(f"{part.name} has no setup sheet {sheet}: open the one its review links"
                             if sheet else f"{part.name} has no setup sheet yet: review it first")
        return json.loads(path.read_text())

    def runs(self, part: Part, *, replies: bool = False) -> int:
        """How many review (or reply) runs this version has had: the next run's number is one more."""
        pattern = re.compile(rf"{part.version_id[:8]}-{'r' if replies else ''}\d+")
        return len({e["run_id"] for e in self._events(part)
                    if e["version_id"] == part.version_id and pattern.fullmatch(e["run_id"])})

    def last_review(self, part: Part) -> Event | None:
        """This version's latest review that was posted: a run that failed first answers nothing (Codex P2)."""
        prefix = f"{part.version_id[:8]}-"
        posted = [int(e["run_id"][len(prefix):]) for e in self._events(part)
                  if e["version_id"] == part.version_id and e["title"] == "Posted the review"
                  and e["run_id"].startswith(prefix) and e["run_id"][len(prefix):].isdigit()]
        return self.store.event(review_key(part, max(posted))) if posted else None

    def _events(self, part: Part) -> list[dict]:
        return self.store.trace_after(part.document_id, 0, limit=1_000_000)

    def _file(self, part: Part, suffix: str) -> Path:
        return self.folder / f"{part.name}.{suffix}"


def _comment_id() -> str:
    return f"c-{uuid.uuid4().hex[:12]}"


def review_key(part: Part, n: int) -> str:
    """The review event's key: production's version:<vid>, with the run, since a local rerun is a new review."""
    return f"version:{part.version_id}#{n}"


class _Trace:
    """ADR 0007 trace events for one run, as modal_app's _Trace writes them; each flush rewrites the part's
    trace.jsonl from the store, so the file always holds the whole document's history in cursor order."""

    def __init__(self, sink: LocalSink, part: Part, run_id: str):
        self.sink, self.part, self.run_id = sink, part, run_id
        self.seq = 0
        self.buf: list[tuple] = []

    def add(self, kind: str, title: str, **detail) -> None:
        self.seq += 1
        self.buf.append((self.seq, kind, title, time.time(), detail))

    def flush(self) -> None:
        for seq, kind, title, ts, detail in self.buf:
            self.sink.store.append_trace(self.part.document_id, self.run_id, seq, kind, title, ts=ts,
                                         version_id=self.part.version_id, detail=detail)
        self.buf = []
        self.sink._file(self.part, "trace.jsonl").write_text(
            "".join(json.dumps(e) + "\n" for e in self.sink._events(self.part)))
