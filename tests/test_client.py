import urllib.request

import pytest

from anville.connectors.onshape.client import OnshapeClient


class _BodyFails:
    """A 200 response whose body read dies mid-download."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        raise TimeoutError("connection dropped")


def test_successful_call_is_counted_even_if_body_read_fails(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: _BodyFails())
    client = OnshapeClient("a", "s", ledger={})
    with pytest.raises(TimeoutError):
        client.get("/documents/d/x/externaldata/y", raw=True)
    assert client.calls_today() == 1
