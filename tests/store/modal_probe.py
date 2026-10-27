"""On-Modal probe for the store: what local tests can't show (PR #4, "Needs a Modal test").

Answers, before anything is wired:
  1. latency  - p50/p95 of volume.commit() after each write, and of one .remote() store call.
  2. durable  - does a write survive the process being SIGKILLed right after it?
                With after_commit=volume.commit, and without (to show whether the gap is real).
                Then PRAGMA integrity_check on the WAL database a fresh container mounts.
  3. dedup    - 20 concurrent duplicate record() + claim() calls through a single-writer
                service (max_containers=1, concurrent inputs on threads): one row, one claim.

Not covered: the rolling-redeploy overlap (needs `modal deploy`, owned by the main session),
and real host loss (SIGKILL kills the process; Modal's runtime may still commit on shutdown).

Isolated: ephemeral app `anville-store-probe`, its own Volume. No Onshape calls.
    Run (from repo root):  modal run tests/store/modal_probe.py
    Clean up:              modal volume delete anville-store-probe
Not collected by pytest (no test_ prefix).
"""

import os
import signal
import statistics
import threading
import time
import uuid

import modal

app = modal.App("anville-store-probe")
image = modal.Image.debian_slim(python_version="3.12").add_local_python_source("anville")
vol = modal.Volume.from_name("anville-store-probe", create_if_missing=True)
DB = "/store/probe.db"


def _pcts(xs: list[float]) -> dict:
    xs = sorted(xs)
    return {"n": len(xs), "p50_ms": round(statistics.median(xs) * 1000, 1),
            "p95_ms": round(xs[int(0.95 * (len(xs) - 1))] * 1000, 1), "max_ms": round(xs[-1] * 1000, 1)}


@app.function(image=image, volumes={"/store": vol}, max_containers=1, timeout=300)
def commit_latency(run: str, n: int) -> dict:
    from anville.store import Store

    commits: list[float] = []

    def commit():
        t = time.perf_counter()
        vol.commit()
        commits.append(time.perf_counter() - t)

    s = Store(DB, after_commit=commit)
    records = []
    for i in range(n):
        t = time.perf_counter()
        s.record(f"probe:lat:{run}:{i}", "docP", {"i": i})
        records.append(time.perf_counter() - t)
    s.close()
    return {"volume_commit": _pcts(commits), "record_incl_commit": _pcts(records)}


@app.function(image=image, volumes={"/store": vol}, max_containers=1, timeout=120, retries=0)
def write_then_die(prefix: str, k: int, commit_each: bool) -> None:
    from anville.store import Store

    s = Store(DB, after_commit=vol.commit if commit_each else None)
    for i in range(k):
        s.record(f"{prefix}{i}", "docP", {"i": i})
    os.kill(os.getpid(), signal.SIGKILL)  # no close(), no clean exit


@app.function(image=image, volumes={"/store": vol}, max_containers=1, timeout=120)
def inspect(prefix: str) -> dict:
    import sqlite3

    vol.reload()
    conn = sqlite3.connect(DB)
    try:
        return {
            "integrity": conn.execute("PRAGMA integrity_check").fetchone()[0],
            "rows": conn.execute("SELECT COUNT(*) FROM events WHERE key LIKE ?", (prefix + "%",)).fetchone()[0],
            "journal_mode": conn.execute("PRAGMA journal_mode").fetchone()[0],
        }
    finally:
        conn.close()


@app.cls(image=image, volumes={"/store": vol}, max_containers=1, timeout=300)
@modal.concurrent(max_inputs=32)
class StoreSvc:
    @modal.enter()
    def open(self):
        from anville.store import Store

        lock = threading.Lock()

        def commit():
            with lock:
                vol.commit()

        self.s = Store(DB, after_commit=commit)

    @modal.method()
    def record(self, key: str, doc: str, payload: dict) -> bool:
        return self.s.record(key, doc, payload)

    @modal.method()
    def claim(self, key: str) -> int | None:
        return self.s.claim(key)

    @modal.method()
    def calls(self, day: str) -> int:
        return self.s.calls(day)


@app.function(image=image, timeout=120)
def remote_rtt(n: int) -> dict:
    """Time .remote() into the store from another container: the hop every webhook and job pays."""
    svc = StoreSvc()
    svc.calls.remote("1970-01-01")  # warm the store container first
    rtts = []
    for _ in range(n):
        t = time.perf_counter()
        svc.calls.remote("1970-01-01")  # read-only, so no volume.commit in the timing
        rtts.append(time.perf_counter() - t)
    return _pcts(rtts)


@app.local_entrypoint()
def main():
    run = uuid.uuid4().hex[:8]
    print(f"run {run}")

    print("1. latency:", commit_latency.remote(run, 50))

    for commit_each in (True, False):
        prefix = f"probe:crash:{run}:{int(commit_each)}:"
        try:
            write_then_die.remote(prefix, 20, commit_each)
            print("   write_then_die returned?! (expected a crash)")
        except Exception as e:  # the container dies; Modal surfaces it as an error
            print(f"   killed as intended ({type(e).__name__})")
        print(f"2. durable (after_commit={'volume.commit' if commit_each else 'None'}), "
              f"expect rows=20:", inspect.remote(prefix))

    svc = StoreSvc()
    key = f"probe:dup:{run}"
    inserted = list(svc.record.map([key] * 20, ["docP"] * 20, [{"i": i} for i in range(20)]))
    tokens = list(svc.claim.map([key] * 20))
    print("3. dedup: inserted", inserted.count(True), "(expect 1); claims",
          sorted(t for t in tokens if t), "(expect [1])")

    print("1b. one .remote() store call, container to container:", remote_rtt.remote(20))
