"""Anville's own Onshape identity through OAuth (ADR 0007, Amendment 1).

The Anville account is on Onshape's free plan, which can't create API keys but can register an OAuth
app. Comments posted with its token carry Anville's name; reads, exports and webhooks stay on the
owner's API keys, so the free account's API allowance goes only to the writes that need the name.

Verified 2026-10-29 against onshape-public.github.io/docs/auth/oauth:
- authorize at https://oauth.onshape.com/oauth/authorize (GET, query parameters);
- tokens from https://oauth.onshape.com/oauth/token (POST, URL-encoded form); a redirect_uri sent
  to authorize must be sent again with the code;
- an access token lasts 60 minutes, and every refresh returns a new refresh token.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from anville.connectors.onshape.client import OnshapeError
from anville.connectors.onshape.events import token_matches

AUTHORIZE_URL = "https://oauth.onshape.com/oauth/authorize"
TOKEN_URL = "https://oauth.onshape.com/oauth/token"
EARLY_S = 120          # refresh this long before the access token expires
KEYS_HEADER = "Anville: "  # names the agent when a comment has to go out on the owner's keys

PostForm = Callable[[str, dict], dict]


@dataclass(frozen=True)
class Grant:
    refresh_token: str
    access_token: str
    expires_at: float  # time.time() seconds
    user: str = ""     # the Onshape account that granted it, for the trace


def authorize_url(client_id: str, redirect_uri: str, state: str) -> str:
    return AUTHORIZE_URL + "?" + urllib.parse.urlencode(
        {"response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri, "state": state})


def _post_form(url: str, form: dict) -> dict:
    req = urllib.request.Request(url, data=urllib.parse.urlencode(form).encode(), method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded",
                                          "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        # The body names the OAuth error (e.g. invalid_grant); it never echoes the client secret.
        raise OnshapeError(e.code, "POST", urllib.parse.urlsplit(url).path,
                           e.read().decode(errors="replace")[:200]) from None


def _grant(reply: dict, now: float, user: str = "") -> Grant:
    return Grant(reply["refresh_token"], reply["access_token"], now + float(reply["expires_in"]), user)


def exchange_code(code: str, client_id: str, client_secret: str, redirect_uri: str, *,
                  post: PostForm = _post_form, now: Callable[[], float] = time.time) -> Grant:
    return _grant(post(TOKEN_URL, {"grant_type": "authorization_code", "code": code, "client_id": client_id,
                                   "client_secret": client_secret, "redirect_uri": redirect_uri}), now())


def refresh(grant: Grant, client_id: str, client_secret: str, *,
            post: PostForm = _post_form, now: Callable[[], float] = time.time) -> Grant:
    reply = post(TOKEN_URL, {"grant_type": "refresh_token", "refresh_token": grant.refresh_token,
                             "client_id": client_id, "client_secret": client_secret})
    return _grant(reply, now(), grant.user)


class GrantStore(Protocol):
    def get_grant(self) -> Grant | None: ...
    def put_grant(self, grant: Grant) -> None: ...


class GrantKeeper:
    """Hands out a live access token, refreshing under one lock.

    Every refresh rotates the refresh token, so two refreshes at once would leave one caller holding a
    dead token. The keeper runs in the single-container store service, so its lock serializes every
    job's refresh.
    """

    def __init__(self, grants: GrantStore, refresh_fn: Callable[[Grant], Grant]):
        self._grants, self._refresh = grants, refresh_fn
        self._lock = threading.Lock()

    def token(self, now: float) -> str | None:
        """The access token, or None before the Anville account has granted access. A failed refresh
        raises and leaves the saved grant as it was."""
        with self._lock:
            grant = self._grants.get_grant()
            if grant is None:
                return None
            if grant.expires_at - EARLY_S > now:
                return grant.access_token
            fresh = self._refresh(grant)
            self._grants.put_grant(fresh)
            return fresh.access_token

    def replace(self, grant: Grant) -> None:
        """Save a new grant under the same lock, so a refresh of the old grant can't land after it and
        overwrite it (Codex, OAuth PR)."""
        with self._lock:
            self._grants.put_grant(grant)


def admin_key_ok(key: str, env: Mapping[str, str]) -> bool:
    """The grant flow's own key (Codex, OAuth PR, P1). The webhook secret sits in the webhook URL, so it
    can't also authorize swapping the posting identity: unset, or equal to it, closes the flow."""
    admin = env.get("ANVILLE_OAUTH_ADMIN_TOKEN", "")
    return bool(admin) and admin != env.get("ANVILLE_WEBHOOK_SECRET") and token_matches(key, admin)


def account_error(me: dict, expected_email: str) -> str:
    """Why the account that completed the flow can't become Anville's identity, or "" if it can.
    `me` is /users/sessioninfo; with no email to compare, refuse rather than guess."""
    if not expected_email:
        return "ANVILLE_ACCOUNT_EMAIL isn't set, so no account can be confirmed as Anville's"
    email = str(me.get("email") or "")
    if not email:
        return "Onshape didn't say which account this is"
    if email.casefold() != expected_email.casefold():
        return "this account isn't the configured Anville account"
    return ""


class Poster(Protocol):
    def post(self, path: str, body: dict | None = None, **kw: Any) -> Any: ...


def post_as_anville(anville: Poster | None, keys: Poster, body: dict) -> tuple[dict, str]:
    """Post a comment as the Anville account, or on the owner's keys under an "Anville:" header.

    Only a missing client, 401 or 403 falls back: those mean the comment wasn't created. Any other
    error is raised, since posting again could make a second comment.
    """
    if anville is not None:
        try:
            return anville.post("/comments", body), "anville"
        except OnshapeError as e:
            if e.status not in (401, 403):
                raise
    return keys.post("/comments", {**body, "message": KEYS_HEADER + body["message"]}), "keys"
