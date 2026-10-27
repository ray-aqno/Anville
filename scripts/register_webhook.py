"""Register the Onshape webhook for each allowlisted document (F8: safe to re-run).

Onshape does not de-duplicate registrations, so existing hooks pointing at our
URL are deleted first. Deploy the Modal app before running this: Onshape sends
a webhook.register trial event and cancels the registration unless it gets 200.

Usage: python3 scripts/register_webhook.py https://<workspace>--anville-web.modal.run
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from anville.connectors.onshape.client import OnshapeClient  # noqa: E402
from anville.connectors.onshape.events import (  # noqa: E402
    COMMENT_CREATE,
    CREATE_VERSION,
    TRANSLATION_COMPLETE,
)
from anville.env import allowed_document_ids, load_env  # noqa: E402

EVENTS = [CREATE_VERSION, COMMENT_CREATE, TRANSLATION_COMPLETE]


def main(base_url: str) -> None:
    env = load_env()
    client = OnshapeClient.from_env(env)
    hook_url = f"{base_url.rstrip('/')}/hook/{env['ANVILLE_WEBHOOK_SECRET']}"
    prefix = f"{base_url.rstrip('/')}/hook/"

    existing = client.get("/webhooks").get("items", [])
    for hook in existing:
        if hook.get("url", "").startswith(prefix):
            client.delete(f"/webhooks/{hook['id']}")
            print(f"deleted old webhook {hook['id']}")

    for did in sorted(allowed_document_ids(env)):
        created = client.post("/webhooks", {
            "events": EVENTS,
            "documentId": did,
            "url": hook_url,
            "options": {"collapseEvents": False},
            "isTransient": False,  # otherwise Onshape cleans it up after inactivity
        })
        print(f"registered webhook {created['id']} for document {did}")

    print(f"Onshape calls today (this process): {client.calls_today()}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
