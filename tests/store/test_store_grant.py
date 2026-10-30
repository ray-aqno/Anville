"""The OAuth grant (ADR 0007, Amendment 1): one row, overwritten on every refresh, durable."""

from anville.connectors.onshape.oauth import Grant
from anville.store import Store

from conftest import scalar


def test_no_grant_until_one_is_saved(store):
    assert store.get_grant() is None


def test_a_grant_round_trips_and_a_refresh_overwrites_it(store, db_path):
    store.put_grant(Grant("r0", "a0", 1000.0, user="Anville"))
    store.put_grant(Grant("r1", "a1", 5000.0, user="Anville"))
    assert store.get_grant() == Grant("r1", "a1", 5000.0, user="Anville")
    assert scalar(db_path, "SELECT COUNT(*) FROM oauth_grant") == 1


def test_saving_a_grant_waits_for_durability(db_path, clock):
    """A rotated refresh token that is lost in a crash kills the grant: it must be persisted."""
    commits = []
    s = Store(db_path, clock=clock, after_commit=lambda: commits.append(1))
    s.put_grant(Grant("r0", "a0", 1000.0))
    assert commits
    s.close()
