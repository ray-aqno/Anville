"""Anville's own identity through Onshape OAuth (ADR 0007, Amendment 1)."""

import threading
import urllib.parse

import pytest

from anville.connectors.onshape.client import OnshapeError
from anville.connectors.onshape.oauth import (
    EARLY_S, Grant, GrantKeeper, account_error, admin_key_ok, authorize_url, exchange_code, post_as_anville, refresh,
)
from tests.store.conftest import race


class Poster:
    """A token endpoint that records each form and answers with the next token pair."""

    def __init__(self):
        self.forms, self.n = [], 0

    def __call__(self, url, form):
        self.forms.append((url, form))
        self.n += 1
        return {"access_token": f"a{self.n}", "refresh_token": f"r{self.n}", "expires_in": 3600}


def test_the_authorize_url_carries_the_client_redirect_and_state():
    url = authorize_url("cid", "https://x.modal.run/oauth/callback", "s1")
    assert url.startswith("https://oauth.onshape.com/oauth/authorize?")
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    assert q == {"response_type": ["code"], "client_id": ["cid"],
                 "redirect_uri": ["https://x.modal.run/oauth/callback"], "state": ["s1"]}


def test_a_code_exchange_and_a_refresh_send_the_documented_forms():
    post = Poster()
    g = exchange_code("c0de", "cid", "sec", "https://x/cb", post=post, now=lambda: 100.0)
    assert g == Grant(refresh_token="r1", access_token="a1", expires_at=3700.0)
    g2 = refresh(g, "cid", "sec", post=post, now=lambda: 200.0)
    assert g2.refresh_token == "r2"  # every refresh rotates the refresh token (Onshape docs)
    (u1, f1), (u2, f2) = post.forms
    assert u1 == u2 == "https://oauth.onshape.com/oauth/token"
    assert f1 == {"grant_type": "authorization_code", "code": "c0de", "client_id": "cid",
                  "client_secret": "sec", "redirect_uri": "https://x/cb"}
    assert f2 == {"grant_type": "refresh_token", "refresh_token": "r1", "client_id": "cid", "client_secret": "sec"}


class MemoryGrants:
    def __init__(self, grant=None):
        self.grant, self.puts = grant, 0

    def get_grant(self):
        return self.grant

    def put_grant(self, grant):
        self.grant, self.puts = grant, self.puts + 1


def test_the_keeper_refreshes_only_near_expiry_and_saves_the_rotated_token():
    grants = MemoryGrants(Grant("r0", "a0", expires_at=1000.0))
    calls = []
    keeper = GrantKeeper(grants, lambda g: calls.append(g) or Grant("r1", "a1", 5000.0))
    assert keeper.token(now=1000.0 - EARLY_S - 1) == "a0" and calls == []
    assert keeper.token(now=1000.0 - EARLY_S + 1) == "a1" and len(calls) == 1
    assert grants.grant.refresh_token == "r1" and grants.puts == 1
    assert GrantKeeper(MemoryGrants(None), lambda g: 1 / 0).token(now=0.0) is None  # no grant yet


def test_racing_jobs_share_one_refresh():
    """Refreshing rotates the refresh token: two refreshes at once would leave one job with a dead
    token. The keeper holds a lock, so one refresh serves every racing caller."""
    grants = MemoryGrants(Grant("r0", "a0", expires_at=0.0))
    calls, gate = [], threading.Event()

    def slow_refresh(g):
        calls.append(g.refresh_token)
        gate.wait(0.2)
        return Grant("r1", "a1", 5000.0)

    keeper = GrantKeeper(grants, slow_refresh)
    tokens = race(8, lambda i: keeper.token(now=100.0))
    assert tokens == ["a1"] * 8 and calls == ["r0"]


def test_a_failed_refresh_raises_and_keeps_the_old_grant():
    grants = MemoryGrants(Grant("r0", "a0", expires_at=0.0))

    def broken(g):
        raise OnshapeError(400, "POST", "/oauth/token", "invalid_grant")

    with pytest.raises(OnshapeError):
        GrantKeeper(grants, broken).token(now=100.0)
    assert grants.grant.refresh_token == "r0" and grants.puts == 0


class FakeClient:
    def __init__(self, fail=None):
        self.fail, self.bodies = fail, []

    def post(self, path, body):
        self.bodies.append((path, body))
        if self.fail:
            raise OnshapeError(self.fail, "POST", path, "no")
        return {"id": "c1"}


def test_comments_post_as_anville_when_the_token_works():
    anville, keys = FakeClient(), FakeClient()
    comment, identity = post_as_anville(anville, keys, {"message": "2 setups"})
    assert (comment["id"], identity) == ("c1", "anville")
    assert anville.bodies == [("/comments", {"message": "2 setups"})] and keys.bodies == []


@pytest.mark.parametrize("anville", [None, FakeClient(fail=401), FakeClient(fail=403)])
def test_without_a_working_token_the_keys_post_it_under_an_anville_header(anville):
    keys = FakeClient()
    comment, identity = post_as_anville(anville, keys, {"message": "2 setups"})
    assert identity == "keys" and keys.bodies == [("/comments", {"message": "Anville: 2 setups"})]


def test_other_errors_are_not_retried_under_the_keys():
    """A 404 or 500 isn't an identity problem; posting again could make a second comment."""
    keys = FakeClient()
    with pytest.raises(OnshapeError):
        post_as_anville(FakeClient(fail=500), keys, {"message": "x"})
    assert keys.bodies == []


def test_a_new_grant_waits_for_an_in_flight_refresh_and_wins():
    """Codex, OAuth PR: a grant saved while the old one is refreshing must not be overwritten when that
    refresh lands, or comments would post as the wrong account."""
    grants = MemoryGrants(Grant("old-r0", "old-a0", expires_at=0.0))
    started, release = threading.Event(), threading.Event()

    def slow_refresh(g):
        started.set()
        release.wait(2)
        return Grant("old-r1", "old-a1", 5000.0)

    keeper = GrantKeeper(grants, slow_refresh)
    refreshing = threading.Thread(target=keeper.token, kwargs={"now": 100.0})
    refreshing.start()
    assert started.wait(2)
    saving = threading.Thread(target=keeper.replace, args=(Grant("new-r0", "new-a0", 9000.0, user="Anville"),))
    saving.start()
    release.set()
    refreshing.join(2), saving.join(2)
    assert grants.grant.refresh_token == "new-r0"


def test_only_the_configured_anville_account_is_saved():
    """Codex, OAuth PR (P1): whoever completes the flow must be the Anville account itself."""
    me = {"name": "Anville", "email": "Anville@Example.com"}
    assert account_error(me, "anville@example.com") == ""
    assert "isn't" in account_error({"email": "someone@else.com"}, "anville@example.com")
    assert account_error({"name": "Anville"}, "anville@example.com")  # no email: refuse, don't guess
    assert account_error(me, "")  # not configured: refuse


def test_the_grant_flow_has_its_own_admin_key():
    """Codex, OAuth PR (P1): the webhook secret sits in the webhook URL, so it can't also authorize
    swapping the posting identity."""
    env = {"ANVILLE_OAUTH_ADMIN_TOKEN": "admin-123", "ANVILLE_WEBHOOK_SECRET": "hook-456"}
    assert admin_key_ok("admin-123", env)
    assert not admin_key_ok("hook-456", env) and not admin_key_ok("", env)
    assert not admin_key_ok("x", {"ANVILLE_OAUTH_ADMIN_TOKEN": "x", "ANVILLE_WEBHOOK_SECRET": "x"})  # reused
    assert not admin_key_ok("", {})  # unset: the flow is closed
