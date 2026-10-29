import datetime as dt
import io
import threading
import time
import urllib.error
import urllib.request

import pytest

from anville.connectors.onshape.client import DailyCapReached, OnshapeClient, OnshapeError
from anville.store import Store


class _BodyFails:
    """A 200 response whose body read dies mid-download."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        raise TimeoutError("connection dropped")


class _Ok(_BodyFails):
    def read(self):
        return b"{}"


def _http_error(code):
    return urllib.error.HTTPError("https://x", code, "err", {}, io.BytesIO(b"nope"))


def _serve(monkeypatch, *responses, latency_s=0.0):
    """urlopen returns or raises each response in turn; returns the list of requests sent.

    The last response repeats. latency_s models the network round trip, which
    is the window a check-then-count cap leaks through.
    """
    sent, queue = [], list(responses)
    lock = threading.Lock()

    def urlopen(req, timeout):
        with lock:
            sent.append(req)
            r = queue.pop(0) if len(queue) > 1 else queue[0]
        time.sleep(latency_s)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return sent


def _client(**kw):
    return OnshapeClient("a", "s", sleep=lambda s: None, **kw)


def test_successful_call_is_counted_even_if_body_read_fails(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: _BodyFails())
    client = OnshapeClient("a", "s", ledger={})
    with pytest.raises(TimeoutError):
        client.get("/documents/d/x/externaldata/y", raw=True)
    assert client.calls_today() == 1


def test_rate_limited_attempts_are_not_counted(monkeypatch):
    sent = _serve(monkeypatch, _http_error(429), _Ok())
    client = _client()

    client.get("/documents")

    assert len(sent) == 2
    assert client.calls_today() == 1


def test_error_response_is_not_counted(monkeypatch):
    _serve(monkeypatch, _http_error(404))
    client = _client()

    with pytest.raises(OnshapeError):
        client.get("/documents/d/missing")
    assert client.calls_today() == 0


def test_call_that_got_no_response_stays_counted(monkeypatch):
    # Onshape may have billed it; overcounting is the safe side of the cap.
    _serve(monkeypatch, urllib.error.URLError("reset"))
    client = _client()

    with pytest.raises(urllib.error.URLError):
        client.get("/documents")
    assert client.calls_today() == 1


def test_cap_reached_sends_nothing(monkeypatch):
    sent = _serve(monkeypatch, _Ok())
    client = _client(daily_cap=1)
    client.get("/documents")

    with pytest.raises(DailyCapReached):
        client.get("/documents")
    assert len(sent) == 1


def _race_through_client(client, n):
    barrier, capped = threading.Barrier(n), []

    def call():
        barrier.wait()
        try:
            client.get("/documents")
        except DailyCapReached:
            capped.append(1)

    threads = [threading.Thread(target=call) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return len(capped)


def test_cap_holds_with_threads_racing_through_a_store_ledger(monkeypatch, tmp_path):
    sent = _serve(monkeypatch, _Ok(), latency_s=0.01)
    store = Store(tmp_path / "anville.db")
    try:
        capped = _race_through_client(_client(daily_cap=5, ledger=store), 30)
    finally:
        store.close()

    assert len(sent) == 5
    assert capped == 25


def test_cap_holds_with_threads_racing_through_the_default_ledger(monkeypatch):
    sent = _serve(monkeypatch, _Ok(), latency_s=0.01)

    _race_through_client(_client(daily_cap=5), 30)

    assert len(sent) == 5


def test_mapping_ledger_keeps_the_spike_key_so_deployed_counts_carry_over(monkeypatch):
    _serve(monkeypatch, _Ok())
    ledger = {}

    _client(ledger=ledger).get("/documents")

    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    assert ledger == {f"calls:{today}": 1}


def test_a_bearer_token_replaces_the_key_pair(monkeypatch):
    """ADR 0007, Amendment 1: Anville's comments go out on its OAuth token, so they carry its name."""
    sent = _serve(monkeypatch, _Ok())
    _client(auth_header="Bearer tok").post("/comments", {"message": "x"})
    assert sent[0].get_header("Authorization") == "Bearer tok"
    sent = _serve(monkeypatch, _Ok())
    _client().get("/documents")
    assert sent[0].get_header("Authorization").startswith("Basic ")
