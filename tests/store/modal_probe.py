"""On-Modal probe for the store: what local tests can't show (PR #4, "Needs a Modal test").

Answers, before anything is wired:
  1. latency  - p50/p95 of volume.commit() after each write, and of one .remote() store call.
  2. durable  - does a write survive the process being SIGKILLed right after it?
                With after_commit=volume.commit, and without (to show whether the gap is real).
                Then PRAGMA integrity_check on the WAL database a fresh container mounts.
  3. dedup    - 20 concurrent duplicate record() + claim() calls through a single-writer
                service (max_containers=1, concurrent inputs on threads): one row, one claim.
  4. rtt      - where a .remote() round trip's time goes: the store service vs a serial
                class with no Volume vs a plain function.
  5. job      - store overhead of one job-shaped run (18 calls through StoreSvc.call), with
                volume.commit only on durable writes.

Not covered: the rolling-redeploy overlap (needs `modal deploy`, owned by the main session),
and real host loss (SIGKILL kills the process; Modal's runtime may still commit on shutdown).

Isolated: ephemeral app `anville-store-probe`, its own Volume. No Onshape calls.
    Run (from repo root):  modal run tests/store/modal_probe.py [--only rtt,job]
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
        s.record(f"{prefix}row:{i}", "docP", {"i": i})
    # Sentinel: proves the worker got past every write. Without it, a write error before
    # the kill would read as "rows lost in the crash".
    s.record(f"{prefix}reached-kill", "docP", {})
    os.kill(os.getpid(), signal.SIGKILL)  # no close(), no clean exit


@app.function(image=image, volumes={"/store": vol}, max_containers=1, timeout=120)
def inspect(prefix: str) -> dict:
    import sqlite3

    vol.reload()
    conn = sqlite3.connect(DB)
    try:
        return {
            "integrity": conn.execute("PRAGMA integrity_check").fetchone()[0],
            "rows": conn.execute("SELECT COUNT(*) FROM events WHERE key LIKE ?", (prefix + "row:%",)).fetchone()[0],
            "reached_kill": conn.execute("SELECT COUNT(*) FROM events WHERE key = ?",
                                         (prefix + "reached-kill",)).fetchone()[0] == 1,
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
    def call(self, op: str, *args, **kw):
        # The wiring plan's shape: one generic method in front of the Store.
        return getattr(self.s, op)(*args, **kw)


@app.cls(image=image, max_containers=1, timeout=300)
class SerialSvc:
    @modal.method()
    def ping(self) -> int:
        return 0


@app.function(image=image, max_containers=1, timeout=300)
def ping_fn() -> int:
    return 0


@app.function(image=image, timeout=300)
def remote_rtt(n: int) -> dict:
    """Time .remote() from another container (the hop every webhook and job pays), per target."""
    targets = {
        "StoreSvc.call (concurrent, Volume, SQLite read)": lambda: StoreSvc().call.remote("calls", "1970-01-01"),
        "SerialSvc.ping (no concurrent, no Volume)": lambda: SerialSvc().ping.remote(),
        "ping_fn (plain function)": lambda: ping_fn.remote(),
    }
    out = {}
    for name, call in targets.items():
        call()  # warm
        rtts = []
        for _ in range(n):
            t = time.perf_counter()
            call()
            rtts.append(time.perf_counter() - t)
        out[name] = _pcts(rtts)
    return out


@app.function(image=image, timeout=300)
def job_overhead(run: str, jobs: int) -> dict:
    """Store calls one review job would make, timed end to end from a job container."""
    svc = StoreSvc().call
    svc.remote("calls", "1970-01-01")  # warm
    durable, job = [], []
    for j in range(jobs):
        key, doc = f"probe:job:{run}:{j}", f"docJ-{run}-{j}"
        t = time.perf_counter()
        svc.remote("record", key, doc, {}, version_id=f"v{j}")  # the webhook's durable write
        durable.append(time.perf_counter() - t)
        t = time.perf_counter()
        token = svc.remote("claim", key)
        for _ in range(3):
            svc.remote("reserve_call", "1970-01-02", 10_000)
        for i in range(10):
            svc.remote("append_trace", doc, f"r{j}", i, "tool_call", f"step {i}")
        svc.remote("is_current", doc, f"v{j}")
        svc.remote("reserve_post", key, "review")
        svc.remote("complete_post", key, "review", f"c{j}")
        svc.remote("finish", key, token, True)
        job.append(time.perf_counter() - t)
    return {"record_durable": _pcts(durable), "job_18_calls": _pcts(job)}


@app.local_entrypoint()
def main(only: str = "latency,durable,dedup,rtt,job", peek: str = ""):
    run = uuid.uuid4().hex[:8]
    steps = set(only.split(","))
    print(f"run {run}, steps {sorted(steps)}")

    if "latency" in steps:
        print("1. latency:", commit_latency.remote(run, 50))

    for commit_each in (True, False) if "durable" in steps else ():
        prefix = f"probe:crash:{run}:{int(commit_each)}:"
        crashed = False
        try:
            write_then_die.remote(prefix, 20, commit_each)
            print("   write_then_die returned?! (expected a crash)")
        except (modal.exception.InternalFailure, modal.exception.RemoteError) as e:
            crashed = True  # how Modal surfaced the SIGKILL in run c8f35196
            print(f"   container died ({type(e).__name__})")
        except Exception as e:  # a write or commit error inside the worker, not the kill
            print(f"   WORKER ERROR before the kill: {type(e).__name__}: {str(e)[:200]}")
        result = inspect.remote(prefix)
        valid = crashed and result["reached_kill"]
        print(f"2. durable (after_commit={'volume.commit' if commit_each else 'None'}), "
              f"expect rows=20:", result,
              "" if valid else "<- NOT a crash-durability result: the worker didn't reach the kill")

    if peek:  # inspect an event left by an earlier run, e.g. --peek probe:dup:<run>
        ev = StoreSvc().call.remote("event", peek)
        print(f"peek {peek}: {ev}; now {time.time():.0f}")

    if "dedup" in steps:
        # Own document per run: an earlier run's unfinished claim on a shared document
        # would block this one (one loop per document), which is correct store behaviour.
        call, key, doc = StoreSvc().call, f"probe:dup:{run}", f"docDup-{run}"
        inserted = list(call.map(["record"] * 20, [key] * 20, [doc] * 20, [{"i": i} for i in range(20)]))
        tokens = list(call.map(["claim"] * 20, [key] * 20))
        print("3. dedup: inserted", inserted.count(True), "(expect 1); claims",
              sorted(t for t in tokens if t), "(expect [1])")

    if "rtt" in steps:
        for name, pcts in remote_rtt.remote(20).items():
            print(f"4. rtt {name}: {pcts}")

    if "job" in steps:
        print("5. job:", job_overhead.remote(run, 5))
