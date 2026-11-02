"""The two seams between Anville's CAD-agnostic core and a CAD system (ADR 0001).

A `CadSource` turns a reference into a `Part`: a STEP file and the ids its reviews are keyed on. A `ReviewSink`
is where the review, the thread and the shop's setup sheet go, and where the run's ADR 0007 trace is kept.
Everything between them (geometry, setup count, the agent, the ledgers) is the core, the same for every CAD.

Implemented: `connectors.local` (a folder of .step files; the review as files). Onshape is the second pair, still
wired inline in modal_app (webhook plus STEP export; comments and the fix branch); RFC 0006 maps other CADs.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class Part:
    name: str
    document_id: str   # what a designer's constraints are scoped to ("doc:<id>"), across versions
    version_id: str    # one saved state of the part: a new one gets a new review
    step: Path


class CadSource(Protocol):
    def part(self, ref: str) -> Part: ...


class ReviewSink(Protocol):
    # Every comment written gets its own id (RFC 0006): the self-loop guard is per comment (ADR 0003 F1).
    def trace(self, part: Part, run_id: str): ...                       # add(kind, title, **detail), flush()
    def post_review(self, part: Part, message: str) -> str: ...         # returns the review's comment id
    def post_reply(self, part: Part, reply_id: str, reply: str, message: str | None) -> str | None: ...  # answer's id
    def is_own(self, comment_id: str) -> bool: ...
    def store_sheet(self, part: Part, sheet_id: str, sheet: dict) -> str: ...  # returns the link the comment gives
