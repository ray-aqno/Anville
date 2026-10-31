"""Panel test helpers: a list-backed trace reader, a recording writer, and a client per fixture."""

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from anville.panel import make_router

FIXTURES = Path(__file__).parent / "fixtures"
SECRET = "s3cret"
DOC = "dSYN"


def load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.synthetic.json").read_text())


def list_reader(events: list[dict], calls: list | None = None):
    """trace_after over a list: the store's semantics (document, cursor > after, oldest first, limit)."""
    def read(document_id: str, after: int, limit: int) -> list[dict]:
        if calls is not None:
            calls.append((document_id, after, limit))
        return [e for e in events if e["document_id"] == document_id and e["cursor"] > after][:limit]
    return read


class RecordingWriter:
    """Store.record over a dict: False on a key it has already seen, like INSERT OR IGNORE."""

    def __init__(self):
        self.rows: dict[str, tuple[str, dict]] = {}

    def __call__(self, key: str, document_id: str, payload: dict, **_) -> bool:
        if key in self.rows:
            return False
        self.rows[key] = (document_id, payload)
        return True


def client_for(events: list[dict], writer=None, **kw) -> TestClient:
    app = FastAPI()
    app.include_router(make_router(list_reader(events), writer or RecordingWriter(), panel_secret=SECRET,
                                   allowed_docs=lambda: {DOC}, **kw))
    return TestClient(app)


@pytest.fixture
def live():
    return load("part_a_live")


@pytest.fixture
def fallback():
    return load("part_a_fallback")


@pytest.fixture
def client(live):
    return client_for(live["events"])
