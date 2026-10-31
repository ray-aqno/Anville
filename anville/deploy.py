"""What the deployed web app needs from the panel. Outside anville.panel and with no third-party imports:
modal_app reads it at deploy time, where only Modal's client is installed (no FastAPI)."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

SHIPPED = frozenset({".py", ".html", ".js", ".css"})


def image_ignore(path: Path) -> bool:
    """Files left out of the image. Modal's default ships only .py files, which drops the panel's page and
    scripts; this keeps those and still leaves out node tooling."""
    path = Path(path)
    return path.suffix not in SHIPPED or "node_modules" in path.parts or "__pycache__" in path.parts


def panel_secret(env: Mapping[str, str]) -> str | None:
    """The panel token, or None to leave the panel unmounted. Unset must not crash web(): the webhook
    endpoint lives there too. The panel token rides in URLs, so it can't equal the webhook secret (in the
    webhook URL) or the OAuth admin token (which /oauth/start accepts; Codex, PR #34)."""
    token = env.get("ANVILLE_PANEL_TOKEN", "")
    taken = {env.get("ANVILLE_WEBHOOK_SECRET"), env.get("ANVILLE_OAUTH_ADMIN_TOKEN")}
    return token if token and token not in taken else None
