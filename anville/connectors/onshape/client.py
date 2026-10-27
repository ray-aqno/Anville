"""Budgeted Onshape REST client (ADR 0001).

Every Onshape call goes through here so the annual quota can't be burned by a
bug. Only 2xx/3xx responses count toward Onshape's limit, so only those are
counted here. Stdlib only, so it runs unchanged locally and inside Modal.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import time
import urllib.error
import urllib.request
from typing import Any, Callable, MutableMapping

API_PREFIX = "/api/v10"


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
        ledger: MutableMapping[str, Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        token = base64.b64encode(f"{access_key}:{secret_key}".encode()).decode()
        self._auth = f"Basic {token}"
        self._base = base_url.rstrip("/")
        self._cap = daily_cap
        # Any dict-like store works: a plain dict locally, a modal.Dict in the cloud.
        self._ledger = ledger if ledger is not None else {}
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
        return int(self._ledger.get(self._ledger_key(), 0))

    def request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        accept: str = "application/json",
        raw: bool = False,
    ) -> Any:
        if self.calls_today() >= self._cap:
            raise DailyCapReached(f"{self.calls_today()} calls today, cap is {self._cap}")

        url = self._base + (path if path.startswith("/api/") else API_PREFIX + path)
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": self._auth, "Accept": accept}
        if data is not None:
            headers["Content-Type"] = "application/json"

        for attempt in range(4):
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    payload = resp.read()
                    self._count()
                    if raw:
                        return payload
                    return json.loads(payload) if payload else None
            except urllib.error.HTTPError as e:
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

    def _ledger_key(self) -> str:
        return "calls:" + dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")

    def _count(self) -> None:
        key = self._ledger_key()
        self._ledger[key] = int(self._ledger.get(key, 0)) + 1
