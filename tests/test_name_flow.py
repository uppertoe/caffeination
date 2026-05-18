from fastapi.testclient import TestClient


def _client() -> TestClient:
    from app.main import create_app

    return TestClient(create_app())


def test_first_visit_shows_name_form():
    with _client() as client:
        r = client.get("/")
    assert r.status_code == 200
    assert "What's your name?" in r.text


def test_set_name_then_dashboard_shows_greeting():
    with _client() as client:
        # First visit primes the cookie + user row.
        client.get("/")
        r = client.post("/me/name", data={"display_name": "Sam"})
        assert r.status_code == 200
        assert "Hi, Sam" in r.text

        # A fresh GET / now skips the name form.
        r = client.get("/")
        assert r.status_code == 200
        assert "What's your name?" not in r.text
        assert "Hi, Sam" in r.text


def test_blank_name_rejected():
    with _client() as client:
        client.get("/")
        r = client.post("/me/name", data={"display_name": "   "})
        assert r.status_code == 422
        assert "Name must be" in r.text
