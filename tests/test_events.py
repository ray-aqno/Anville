from anville.connectors.onshape.events import (
    COMMENT_CREATE,
    CREATE_VERSION,
    TRANSLATION_COMPLETE,
    classify,
)

DOC = "doc1"


def run(event, self_ids=(), seen=()):
    return classify(event, {DOC}, set(self_ids), set(seen))


def test_handshake_is_acked_even_without_document():
    assert run({"event": "webhook.register"}).action == "ack"
    assert run({"event": "webhook.ping"}).action == "ack"


def test_new_version_on_allowlisted_doc_is_processed():
    d = run({"event": CREATE_VERSION, "documentId": DOC, "versionId": "v1"})
    assert (d.action, d.idempotency_key) == ("process", "version:v1")


def test_document_not_on_allowlist_is_ignored():  # F2
    assert run({"event": CREATE_VERSION, "documentId": "other", "versionId": "v1"}).action == "ignore"


def test_missing_document_id_is_ignored():
    assert run({"event": CREATE_VERSION, "versionId": "v1"}).action == "ignore"


def test_version_created_by_anville_is_ignored():  # F1
    d = run({"event": CREATE_VERSION, "documentId": DOC, "versionId": "v1"}, self_ids={"v1"})
    assert d.action == "ignore" and "F1" in d.reason


def test_comment_created_by_anville_is_ignored():  # F1
    d = run({"event": COMMENT_CREATE, "documentId": DOC, "commentId": "c1"}, self_ids={"c1"})
    assert d.action == "ignore" and "F1" in d.reason


def test_designer_comment_is_processed():
    d = run({"event": COMMENT_CREATE, "documentId": DOC, "commentId": "c2"})
    assert (d.action, d.idempotency_key) == ("process", "comment:c2")


def test_duplicate_delivery_is_ignored():
    d = run({"event": CREATE_VERSION, "documentId": DOC, "versionId": "v1"}, seen={"version:v1"})
    assert d.action == "ignore" and d.reason == "duplicate delivery"


def test_events_missing_their_id_are_ignored():
    assert run({"event": CREATE_VERSION, "documentId": DOC}).action == "ignore"
    assert run({"event": COMMENT_CREATE, "documentId": DOC}).action == "ignore"
    assert run({"event": TRANSLATION_COMPLETE}).action == "ignore"


def test_translation_complete_is_processed_without_allowlist():
    d = run({"event": TRANSLATION_COMPLETE, "translationId": "t1"})
    assert (d.action, d.idempotency_key) == ("process", "tx:t1")


def test_unknown_event_is_ignored():
    assert run({"event": "onshape.document.lifecycle.shared", "documentId": DOC}).action == "ignore"
