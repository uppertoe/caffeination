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
    assert "coffee-rch" in r.text
    assert "coffee_rch_id" in r.cookies
