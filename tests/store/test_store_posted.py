"""ADR 0006 §6 idempotent side effects: reserve before posting, complete after."""

import pytest
from conftest import race


def test_twenty_racing_jobs_reserve_one_post(store):
    won = race(20, lambda i: store.reserve_post("version:v1", "review"))

    assert won.count(True) == 1


def test_a_reserved_but_unconfirmed_post_counts_as_posted(store):
    # A crash between reserve and complete must not lead to a second comment.
    assert store.already_posted("version:v1", "review") is False
    store.reserve_post("version:v1", "review")

    assert store.already_posted("version:v1", "review") is True


def test_kinds_of_post_for_one_event_are_independent(store):
    store.reserve_post("version:v1", "review")

    assert store.already_posted("version:v1", "setup_sheet") is False
    assert store.reserve_post("version:v1", "setup_sheet") is True


def test_unconfirmed_posts_are_listed_until_completed(store, clock):
    store.reserve_post("version:v1", "review")
    store.reserve_post("version:v2", "review")
    clock.advance(61)
    store.complete_post("version:v2", "review", comment_id="c-9")

    assert store.unconfirmed_posts(older_than_s=60) == [("version:v1", "review")]


def test_fresh_reservations_are_not_yet_unconfirmed(store, clock):
    store.reserve_post("version:v1", "review")
    clock.advance(59)

    assert store.unconfirmed_posts(older_than_s=60) == []


def test_completing_a_post_that_was_never_reserved_is_an_error(store):
    with pytest.raises(KeyError):
        store.complete_post("version:v1", "review", comment_id="c-1")


def test_a_settled_record_is_stored_done_and_never_reaped(store, clock):
    """A setup sheet is stored as an event so the panel can load it, but there's nothing to run."""
    assert store.record("sheet:abc", "d1", {"schema": "anville.setup_sheet/1"}, settled=True)
    clock.advance(3600)
    assert store.event("sheet:abc").payload["schema"] == "anville.setup_sheet/1"
    assert [e.key for e in store.reapable(60)] == []
    assert store.claim("sheet:abc") is None


def test_a_posted_comment_leads_back_to_the_event_that_posted_it(store):
    """ADR 0012: a reply's parentId is how Anville finds the review it answers."""
    store.reserve_post("version:v1", "review")
    store.complete_post("version:v1", "review", "c-root")
    assert store.post_for_comment("c-root") == ("version:v1", "review")
    assert store.post_for_comment("c-unknown") is None
    store.reserve_post("version:v2", "review")  # reserved, never confirmed: no comment id to match
    assert store.post_for_comment("") is None
