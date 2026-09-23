"""The startup warm-up must run cleanly against an empty database and leave
the templates compiled, so the first visitor is not the one paying for it."""

import os

from fastapi.testclient import TestClient


def test_warm_up_compiles_every_template_and_serves_first_request(monkeypatch):
    monkeypatch.setenv("WARM_ON_STARTUP", "true")
    from app.config import get_settings

    get_settings.cache_clear()
    from app.main import create_app, templates

    templates.env.cache.clear()
    with TestClient(create_app()) as c:
        # Lifespan ran warm_up(): every template is already in Jinja's cache.
        html_templates = [n for n in templates.env.list_templates() if n.endswith(".html")]
        assert len(templates.env.cache) >= len(html_templates)
        # Nothing was written by the transient warm-up user.
        r = c.get("/")
        assert r.status_code == 200
        assert "warm-up" not in r.text


def test_warm_up_is_off_in_the_suite():
    from app.config import get_settings

    assert os.environ["WARM_ON_STARTUP"] == "false"
    assert get_settings().warm_on_startup is False
