"""Budgeted Onshape REST client (ADR 0001).

Every Onshape call goes through here so the annual quota can't be burned by a
bug. Only 2xx/3xx responses count toward Onshape's limit, so a 4xx/5xx hands
its reservation back; a call that got no response stays counted, since it may
have been billed. Stdlib only, so it runs unchanged locally and inside Modal.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable, MutableMapping, Protocol

API_PREFIX = "/api/v10"


class CallLedger(Protocol):
    """Per-day call budget. reserve_call must check and increment atomically.

    anville.store.Store implements this for the deployed app (one budget across
    containers); MemoryLedger is the single-process default.
    """

    def reserve_call(self, day: str, cap: int) -> bool: ...
    def release_call(self, day: str) -> None: ...
    def calls(self, day: str) -> int: ...


class MemoryLedger:
    """Atomic within one process. Fine locally and in tests, not across containers."""

    def __init__(self) -> None:
        self._n: dict[str, int] = {}
        self._lock = threading.Lock()

    def reserve_call(self, day: str, cap: int) -> bool:
        with self._lock:
            if self._n.get(day, 0) >= cap:
                return False
            self._n[day] = self._n.get(day, 0) + 1
            return True

    def release_call(self, day: str) -> None:
        with self._lock:
            self._n[day] = max(0, self._n.get(day, 0) - 1)

    def calls(self, day: str) -> int:
        return self._n.get(day, 0)


class MappingLedger:
    """Adapter for a dict-like store (the spike's modal.Dict), keyed `calls:<day>`.

    NOT atomic across containers: two jobs can both read n < cap. Kept only so
    the deployed spike runs unchanged until modal_app passes the Store instead.
    """

    def __init__(self, mapping: MutableMapping[str, Any]):
        self._m = mapping

    def reserve_call(self, day: str, cap: int) -> bool:
        n = int(self._m.get(f"calls:{day}", 0))
        if n >= cap:
            return False
        self._m[f"calls:{day}"] = n + 1
        return True

    def release_call(self, day: str) -> None:
        self._m[f"calls:{day}"] = max(0, int(self._m.get(f"calls:{day}", 0)) - 1)

    def calls(self, day: str) -> int:
        return int(self._m.get(f"calls:{day}", 0))


class OnshapeError(Exception):
    def __init__(self, status: int, method: str, path: str, body: str):
        super().__init__(f"{method} {path} -> HTTP {status}: {body[:300]}")
        self.status = status


class DailyCapReached(Exception):
    """Our own per-day cap (ONSHAPE_DAILY_CALL_CAP) was hit; nothing was sent."""


class QuotaExhausted(OnshapeError):
    """Onshape returned 402: the annual API quota is used up. Hard stop."""


class OnshapeClient:
    def __init__(
        self,
        access_key: str,
        secret_key: str,
        base_url: str = "https://cad.onshape.com",
        daily_cap: int = 100,
        ledger: CallLedger | MutableMapping[str, Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        auth_header: str | None = None,
    ):
        # auth_header: "Bearer <token>" to act as the Anville account (ADR 0007, Amendment 1).
        token = base64.b64encode(f"{access_key}:{secret_key}".encode()).decode()
        self._auth = auth_header or f"Basic {token}"
        self._base = base_url.rstrip("/")
        self._cap = daily_cap
        if ledger is None:
            ledger = MemoryLedger()
        elif not hasattr(ledger, "reserve_call"):
            ledger = MappingLedger(ledger)
        self._ledger: CallLedger = ledger
        self._sleep = sleep

    @classmethod
    def from_env(cls, env: MutableMapping[str, str], **kw: Any) -> "OnshapeClient":
        return cls(
            env["ONSHAPE_ACCESS_KEY"],
            env["ONSHAPE_SECRET_KEY"],
            env.get("ONSHAPE_BASE_URL") or "https://cad.onshape.com",
            int(env.get("ONSHAPE_DAILY_CALL_CAP") or 100),
            **kw,
        )

    def calls_today(self) -> int:
        return self._ledger.calls(_utc_day())

    def request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        accept: str = "application/json",
        raw: bool = False,
    ) -> Any:
        url = self._base + (path if path.startswith("/api/") else API_PREFIX + path)
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": self._auth, "Accept": accept}
        if data is not None:
            headers["Content-Type"] = "application/json"

        for attempt in range(4):
            # Reserve before sending, atomically, so racing jobs can't overspend the cap.
            # The reservation stands unless Onshape answers 4xx/5xx: a failed body read or
            # a call with no response may still have been billed.
            day = _utc_day()
            if not self._ledger.reserve_call(day, self._cap):
                raise DailyCapReached(f"{self._ledger.calls(day)} calls today, cap is {self._cap}")
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    payload = resp.read()
                    if raw:
                        return payload
                    return json.loads(payload) if payload else None
            except urllib.error.HTTPError as e:
                if e.code >= 400:
                    # Onshape bills only 2xx/3xx; 4xx/5xx (429 included) are free, so hand it back.
                    self._ledger.release_call(day)
                text = e.read().decode(errors="replace")
                if e.code == 429 and attempt < 3:
                    # Back off 2s, 4s, 8s; 429s don't count toward the quota.
                    self._sleep(2 ** (attempt + 1))
                    continue
                if e.code == 402:
                    raise QuotaExhausted(e.code, method, path, text) from None
                raise OnshapeError(e.code, method, path, text) from None
        raise AssertionError("unreachable")

    def get(self, path: str, **kw: Any) -> Any:
        return self.request("GET", path, **kw)

    def post(self, path: str, body: dict | None = None, **kw: Any) -> Any:
        return self.request("POST", path, body=body, **kw)

    def delete(self, path: str) -> Any:
        return self.request("DELETE", path)


def _utc_day() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
