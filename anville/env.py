"""Minimal .env reader for local scripts (Modal injects the same vars via Secret.from_dotenv)."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def load_env(path: Path = REPO_ROOT / ".env") -> dict[str, str]:
    env: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip()
    return env


def allowed_document_ids(env: dict[str, str]) -> set[str]:
    return {d.strip() for d in env.get("ANVILLE_ALLOWED_DOCUMENT_IDS", "").split(",") if d.strip()}
