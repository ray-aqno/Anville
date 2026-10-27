import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest

from anville.store import Store


class FakeClock:
    def __init__(self, t: float = 1_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "anville.db"


@pytest.fixture
def store(db_path, clock):
    s = Store(db_path, clock=clock)
    yield s
    s.close()


def race(n, fn):
    """Run fn(0..n-1) on n threads released together by a barrier."""
    barrier = threading.Barrier(n)

    def run(i):
        barrier.wait()
        return fn(i)

    with ThreadPoolExecutor(n) as pool:
        return list(pool.map(run, range(n)))


def scalar(db_path, sql, *args):
    """Read straight from the file, bypassing the Store, so assertions don't trust its reads."""
    with closing(sqlite3.connect(db_path)) as conn:
        return conn.execute(sql, args).fetchone()[0]
