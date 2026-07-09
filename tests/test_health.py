from fastapi.testclient import TestClient


def _client() -> TestClient:
    # Import inside the function so the conftest fixture has already patched env.
    from app.main import create_app

    return TestClient(create_app())


def test_healthz_ok():
    with _client() as client:
        r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_index_renders_and_sets_cookie():
    with _client() as client:
        r = client.get("/")
    assert r.status_code == 200
    assert "caffeine@RCH" in r.text
    assert "coffee_rch_id" in r.cookies


def test_index_configures_htmx_to_swap_4xx_responses():
    """Validation errors come back as 409/422 fragments; htmx 2 drops 4xx
    responses unless the config says to swap them. Without this meta tag,
    every 'name taken' / 'unknown drink' error is invisible in the browser."""
    with _client() as client:
        r = client.get("/")
    assert 'name="htmx-config"' in r.text
    assert '{"code":"[4]..","swap":true}' in r.text


def test_webmanifest_uses_app_name(monkeypatch):
    monkeypatch.setenv("APP_NAME", "caffeine@Monash")
    from app.config import get_settings

    get_settings.cache_clear()
    with _client() as client:
        r = client.get("/site.webmanifest")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/manifest+json"
    body = r.json()
    assert body["name"] == "caffeine@Monash"
    assert body["short_name"] == "caffeine@Monash"
