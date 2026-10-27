"""ADR 0006 version guard: a result for a superseded version is discarded, not posted."""


def test_older_version_is_stale_once_a_newer_one_is_recorded(store):
    store.record("version:v1", "docA", {}, version_id="v1", version_ts=100.0)
    store.record("version:v2", "docA", {}, version_id="v2", version_ts=200.0)

    assert store.is_current("docA", "v1") is False
    assert store.is_current("docA", "v2") is True


def test_late_delivery_of_an_older_version_does_not_take_over(store):
    store.record("version:v2", "docA", {}, version_id="v2", version_ts=200.0)
    store.record("version:v1", "docA", {}, version_id="v1", version_ts=100.0)

    assert store.is_current("docA", "v2") is True
    assert store.is_current("docA", "v1") is False


def test_without_an_onshape_timestamp_arrival_order_decides(store, clock):
    store.record("version:v1", "docA", {}, version_id="v1")
    clock.advance(1)
    store.record("version:v2", "docA", {}, version_id="v2")

    assert store.is_current("docA", "v1") is False


def test_versions_on_different_documents_do_not_supersede_each_other(store):
    store.record("version:a1", "docA", {}, version_id="a1", version_ts=100.0)
    store.record("version:b1", "docB", {}, version_id="b1", version_ts=200.0)

    assert store.is_current("docA", "a1") is True
    assert store.is_current("docB", "b1") is True


def test_comment_events_do_not_move_the_current_version(store):
    store.record("version:v1", "docA", {}, version_id="v1", version_ts=100.0)
    store.record("comment:c1", "docA", {})

    assert store.is_current("docA", "v1") is True


def test_document_with_no_recorded_version_has_nothing_newer(store):
    assert store.is_current("docA", "v1") is True
