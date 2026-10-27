"""The duplicate-delivery race across OS processes, not just threads.

Threads in one process share SQLite's in-process lock table; separate
processes contend on the file locks themselves, which is closer to separate
containers. Still local disk: a Modal Volume is a different filesystem, so
this does not stand in for the on-Modal test.
"""

import subprocess
import sys
import time
from pathlib import Path

from conftest import scalar

REPO = Path(__file__).resolve().parents[2]

WORKER = """
import sys, time
from anville.store import Store
path, start_at, i = sys.argv[1], float(sys.argv[2]), sys.argv[3]
s = Store(path)
time.sleep(max(0.0, start_at - time.time()))
inserted = s.record("version:v1", "docA", {"delivery": i})
token = s.claim("version:v1")
print(int(inserted), token or 0)
s.close()
"""


def test_twenty_processes_delivering_one_event_insert_one_row_and_one_claim(db_path):
    start_at = time.time() + 1.0  # let every interpreter boot, then release them together
    procs = [
        subprocess.Popen([sys.executable, "-c", WORKER, str(db_path), str(start_at), str(i)],
                         cwd=REPO, env={"PYTHONPATH": str(REPO)},
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for i in range(20)
    ]
    results = []
    for p in procs:
        out, err = p.communicate(timeout=30)
        assert p.returncode == 0, err
        results.append(tuple(int(x) for x in out.split()))

    assert sum(inserted for inserted, _ in results) == 1
    assert sorted(token for _, token in results if token) == [1]
    assert scalar(db_path, "SELECT COUNT(*) FROM events") == 1
