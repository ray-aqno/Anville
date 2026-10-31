"""What the deployed web app needs from the panel: its files in the image, and a token that's safe to mount."""

from anville.deploy import image_ignore, panel_secret
from anville.panel.router import ASSETS, STATIC


def test_the_image_ships_every_file_the_panel_serves():
    """Modal's add_local_python_source ships only .py files by default (modal/_image.py:962,
    NON_PYTHON_FILES), so /panel would 404 its own page and scripts once deployed."""
    for name in {"panel.html", *ASSETS}:
        assert (STATIC / name).exists() and not image_ignore(STATIC / name), name
    assert not image_ignore(STATIC.parent / "router.py")
    assert image_ignore(STATIC / "package.json") and image_ignore(STATIC / "node_modules" / "x" / "y.js")


def test_the_panel_mounts_only_with_its_own_token():
    hook = {"ANVILLE_WEBHOOK_SECRET": "w"}
    assert panel_secret(hook) is None  # unset: leave it unmounted rather than crash web()
    assert panel_secret({**hook, "ANVILLE_PANEL_TOKEN": "w"}) is None  # the webhook URL already carries it
    assert panel_secret({**hook, "ANVILLE_PANEL_TOKEN": ""}) is None
    assert panel_secret({**hook, "ANVILLE_PANEL_TOKEN": "p"}) == "p"


def test_the_deploy_helpers_import_without_fastapi():
    """modal_app imports them at deploy time, where FastAPI isn't installed."""
    import subprocess
    import sys

    code = ("import sys; sys.modules['fastapi'] = None; import anville.deploy; "
            "assert 'anville.panel' not in sys.modules")
    assert subprocess.run([sys.executable, "-c", code], capture_output=True).returncode == 0


def test_the_panel_token_is_never_the_oauth_admin_token():
    """Codex, PR #34: the panel token rides in URLs, and /oauth/start accepts the admin token."""
    env = {"ANVILLE_WEBHOOK_SECRET": "w", "ANVILLE_OAUTH_ADMIN_TOKEN": "a"}
    assert panel_secret({**env, "ANVILLE_PANEL_TOKEN": "a"}) is None
    assert panel_secret({**env, "ANVILLE_PANEL_TOKEN": "p"}) == "p"


def test_the_sheet_link_is_signed_and_needs_a_panel_token():
    """The comment's link to the traveler (demo gap 3): /sheet/<id>?s=<sig>, on the deployed web app."""
    from anville.deploy import sheet_link
    from anville.panel import sheet_sig

    env = {"ANVILLE_PANEL_TOKEN": "p" * 32, "ANVILLE_WEBHOOK_SECRET": "w" * 32,
           "ANVILLE_PUBLIC_URL": "https://example--anville-web.modal.run/"}
    link = sheet_link(env, "ab12")
    assert link == f"https://example--anville-web.modal.run/sheet/ab12?s={sheet_sig('p' * 32, 'ab12')}"
    assert sheet_link({**env, "ANVILLE_PANEL_TOKEN": ""}, "ab12") is None  # no traveler mounted: no link
    assert sheet_link(env, None) is None
