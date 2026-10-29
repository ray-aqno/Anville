"""Chat model client for the agent loop (ADR 0009 §7). Stdlib only; OpenAI-compatible.

One client serves both hosts: Muse Spark Open on Baseten and muse-spark-1.3 on the Meta Model API.
Lessons from the probe (ADR 0003 Amendment 1) are built in: max_tokens covers reasoning plus
output, so it never drops below 4096; finish_reason is kept so a budget failure isn't mistaken for
a model failure; arguments are parsed as strict JSON (no NaN/Infinity).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Mapping, Protocol

MIN_MAX_TOKENS = 4096
META_API = "https://api.meta.ai/v1"


@dataclass
class Reply:
    """One model turn, reduced to what the loop needs."""

    tool: str | None                # name of the single tool call, if exactly one was made
    arguments: dict | None          # parsed arguments of that call
    finish_reason: str | None
    content: str | None = None
    error: str | None = None        # why this reply is unusable (too many calls, bad JSON, ...)
    usage: dict = field(default_factory=dict)


class ChatModel(Protocol):
    name: str

    def call(self, messages: list[dict], tools: list[dict], *, max_tokens: int | None = None,
             reasoning_effort: str | None = None) -> Reply: ...


class ModelUnavailable(Exception):
    """The host refused or failed the request (HTTP error, timeout). The stage falls back."""


def _reject_constant(name: str):
    raise ValueError(f"non-standard JSON constant {name}")


def parse_reply(response: dict) -> Reply:
    choice = response["choices"][0]
    msg = choice.get("message") or {}
    calls = msg.get("tool_calls") or []
    reply = Reply(tool=None, arguments=None, finish_reason=choice.get("finish_reason"),
                  content=msg.get("content"), usage=response.get("usage") or {})
    if reply.finish_reason == "length":
        reply.error = "ran out of tokens while reasoning (budget failure)"
        return reply
    if len(calls) != 1:
        reply.error = f"expected exactly one tool call, got {len(calls)}"
        return reply
    fn = calls[0].get("function") or {}
    raw = fn.get("arguments")
    try:
        args = json.loads(raw, parse_constant=_reject_constant) if isinstance(raw, str) else raw
    except ValueError as e:  # JSONDecodeError is a ValueError
        reply.error = f"arguments are not valid JSON: {e}"
        return reply
    if not isinstance(args, dict):
        reply.error = "arguments are not a JSON object"
        return reply
    reply.tool, reply.arguments = fn.get("name"), args
    return reply


class OpenAICompatModel:
    def __init__(self, base_url: str, api_key: str, model: str, *, max_tokens: int = MIN_MAX_TOKENS,
                 reasoning_effort: str | None = None, timeout_s: float = 120):
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._key = api_key
        self.name = model
        self._max_tokens = max(max_tokens, MIN_MAX_TOKENS)
        self._effort = reasoning_effort
        self._timeout = timeout_s

    @classmethod
    def from_env(cls, env: Mapping[str, str], which: str = "open") -> "OpenAICompatModel":
        """`open`: Muse Spark Open on Baseten. `spark`: muse-spark-1.3 on the Meta Model API, at low effort."""
        if which == "open":
            return cls(env["MUSE_SPARK_OPEN_BASE_URL"], env["MUSE_SPARK_OPEN_API_KEY"],
                       env["MUSE_SPARK_OPEN_MODEL_ID"])
        if which == "spark":
            return cls(META_API, env["MODEL_API_KEY"], env["MUSE_SPARK_MODEL_ID"], reasoning_effort="low")
        raise ValueError(f"unknown model {which!r}")

    def call(self, messages: list[dict], tools: list[dict], *, max_tokens: int | None = None,
             reasoning_effort: str | None = None) -> Reply:
        body = {"model": self.name, "messages": messages, "tools": tools, "tool_choice": "auto",
                "max_tokens": max(max_tokens or self._max_tokens, MIN_MAX_TOKENS)}
        effort = reasoning_effort or self._effort
        if effort:
            # Meta Model API, and Baseten for Spark Open: verified 2026-10-29, "low" cut Spark Open's
            # completion tokens 4-6x on a real review.
            body["reasoning_effort"] = effort
        req = urllib.request.Request(
            self._url, data=json.dumps(body).encode(), method="POST",
            headers={"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            raise ModelUnavailable(f"HTTP {e.code}: {e.read().decode(errors='replace')[:200]}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            raise ModelUnavailable(repr(e)[:200]) from None
        try:
            return parse_reply(json.loads(raw))
        except (ValueError, KeyError, IndexError, TypeError, AttributeError) as e:
            # A 200 with a body we can't read is still an unusable host reply: the stage falls back.
            raise ModelUnavailable(f"malformed response ({type(e).__name__}): {raw[:120]!r}") from None
