"""Atomic Onshape call cap (Greptile P1 deferred from PR #1)."""

from conftest import race


def test_cap_holds_with_fifty_threads_racing_for_ten_calls(store):
    granted = race(50, lambda i: store.reserve_call("2026-10-27", cap=10))

    assert granted.count(True) == 10
    assert store.calls("2026-10-27") == 10


def test_released_call_frees_a_slot(store):
    assert store.reserve_call("2026-10-27", cap=1) is True
    assert store.reserve_call("2026-10-27", cap=1) is False

    store.release_call("2026-10-27")

    assert store.reserve_call("2026-10-27", cap=1) is True


def test_release_never_drives_the_count_negative(store):
    store.release_call("2026-10-27")

    assert store.calls("2026-10-27") == 0


def test_each_day_has_its_own_budget(store):
    store.reserve_call("2026-10-27", cap=1)

    assert store.reserve_call("2026-10-28", cap=1) is True
    assert store.calls("2026-10-27") == 1
