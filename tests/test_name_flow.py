from fastapi.testclient import TestClient


def _client() -> TestClient:
    from app.main import create_app

    return TestClient(create_app())


def test_first_visit_shows_onboarding():
    with _client() as client:
        r = client.get("/")
    assert r.status_code == 200
    assert "Welcome to caffeine@RCH" in r.text


def test_set_name_then_dashboard_shows_greeting():
    with _client() as client:
        # First visit primes the cookie + user row.
        client.get("/")
        r = client.post("/me/name", data={"display_name": "Sam"})
        assert r.status_code == 200
        assert "Hi, Sam" in r.text

        # A fresh GET / now skips the onboarding view.
        r = client.get("/")
        assert r.status_code == 200
        assert "Welcome to caffeine@RCH" not in r.text
        assert "Hi, Sam" in r.text


def test_blank_name_rejected():
    with _client() as client:
        client.get("/")
        r = client.post("/me/name", data={"display_name": "   "})
        assert r.status_code == 422
        assert "Name must be" in r.text


def test_logout_returns_to_onboarding():
    with _client() as client:
        client.get("/")
        client.post("/me/name", data={"display_name": "Sam"})
        client.post("/me/drink", data={"base_id": "latte", "size": "regular", "milk": "oat"})

        r = client.post("/logout")
        assert r.status_code == 200
        assert "Welcome to caffeine@RCH" in r.text

        # Cookie cleared → a fresh GET / starts a new (unnamed) session...
        r = client.get("/")
        assert "Welcome to caffeine@RCH" in r.text
        assert "Hi, Sam" not in r.text

        # ...but Sam's row survives, so they're still claimable from the list.
        assert "Sam" in r.text


def test_duplicate_name_rejected_case_insensitive():
    """Two visitors can't both claim 'Alice' — the second has to claim or pick another."""
    with _client() as alice, _client() as wannabe:
        alice.get("/")
        alice.post("/me/name", data={"display_name": "Alice"})

        wannabe.get("/")
        r = wannabe.post("/me/name", data={"display_name": "ALICE"})
    assert r.status_code == 409
    assert "already taken" in r.text


def test_claim_existing_user_rebinds_cookie():
    """A visitor with a fresh cookie can claim Alice's identity and inherit her drink."""
    with _client() as alice, _client() as visitor:
        alice.get("/")
        alice.post("/me/name", data={"display_name": "Alice"})
        alice.post(
            "/me/drink",
            data={"base_id": "flat_white", "size": "large", "milk": "oat"},
        )

        from sqlmodel import Session, select

        from app.db import get_engine
        from app.models import User

        with Session(get_engine()) as s:
            alice_id = s.exec(
                select(User).where(User.display_name == "Alice")
            ).first().id

        visitor.get("/")
        r = visitor.post(f"/onboard/claim/{alice_id}")
        assert r.status_code == 200
        # Visitor now sees Alice's dashboard with her saved drink.
        assert "Hi, Alice" in r.text
        assert "large oat flat white" in r.text

        # Subsequent GET / for the visitor lands on Alice's dashboard too.
        r = visitor.get("/")
        assert "Hi, Alice" in r.text


def test_claim_nonexistent_user_returns_404():
    with _client() as client:
        client.get("/")
        r = client.post("/onboard/claim/nonexistent-id")
    assert r.status_code == 404


def test_claim_unnamed_user_returns_404():
    """You can't claim an empty user row — only named users are claimable."""
    with _client() as ghost, _client() as visitor:
        # Ghost has a cookie + user row but no display_name.
        ghost.get("/")
        # Get the ghost's user id.
        from sqlmodel import Session, select

        from app.db import get_engine
        from app.models import User

        with Session(get_engine()) as s:
            ghost_id = s.exec(
                select(User).where(User.display_name.is_(None))
            ).first().id

        visitor.get("/")
        r = visitor.post(f"/onboard/claim/{ghost_id}")
    assert r.status_code == 404
