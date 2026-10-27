"""Decide what to do with an incoming Onshape webhook event (ADR 0003 trust boundary).

Pure function, no I/O, so the F1/F2 rules are unit-tested rather than trusted.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Collection, Mapping

HANDSHAKE_EVENTS = {"webhook.register", "webhook.unregister", "webhook.ping"}
CREATE_VERSION = "onshape.model.lifecycle.createversion"
COMMENT_CREATE = "onshape.comment.create"
TRANSLATION_COMPLETE = "onshape.model.translation.complete"


@dataclass(frozen=True)
class Decision:
    action: str  # "ack" | "ignore" | "process"
    reason: str
    idempotency_key: str | None = None


def token_matches(token: str, secret: str) -> bool:
    """Constant-time check of the URL token (F2).

    Compares bytes: hmac.compare_digest raises TypeError on non-ASCII str, which
    would turn a probe like /hook/%C3%A9 into a 500 instead of a 404.
    """
    return hmac.compare_digest(token.encode(), secret.encode())


def classify(
    event: Mapping,
    allowed_document_ids: Collection[str],
    self_created_ids: Collection[str],
    seen_keys: Collection[str],
) -> Decision:
    name = event.get("event", "")

    if name in HANDSHAKE_EVENTS:
        # Must answer 200 or Onshape cancels the registration.
        return Decision("ack", f"handshake {name}")

    if name == TRANSLATION_COMPLETE:
        # Translation events are tied to our own export requests, not to a designer action.
        tid = event.get("translationId")
        if not tid:
            return Decision("ignore", "translation event without translationId")
        key = f"tx:{tid}"
        if key in seen_keys:
            return Decision("ignore", "duplicate delivery", key)
        return Decision("process", "translation complete", key)

    did = event.get("documentId")
    if not did or did not in allowed_document_ids:
        return Decision("ignore", "document not allowlisted")

    if name == CREATE_VERSION:
        vid = event.get("versionId")
        if not vid:
            return Decision("ignore", "createversion without versionId")
        if vid in self_created_ids:
            return Decision("ignore", "version created by Anville (F1)")
        key = f"version:{vid}"
    elif name == COMMENT_CREATE:
        cid = event.get("commentId")
        if not cid:
            return Decision("ignore", "comment event without commentId")
        if cid in self_created_ids:
            return Decision("ignore", "comment created by Anville (F1)")
        key = f"comment:{cid}"
    else:
        return Decision("ignore", f"unhandled event {name or '<missing>'}")

    if key in seen_keys:
        return Decision("ignore", "duplicate delivery", key)
    return Decision("process", name, key)
