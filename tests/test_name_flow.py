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
    with _client() as visitor:
        visitor.get("/")
        # A legacy unnamed row (new code never persists these).
        from sqlmodel import Session

        from app.db import get_engine
        from app.models import User

        with Session(get_engine()) as s:
            s.add(User(id="ghost-row"))
            s.commit()

        r = visitor.post("/onboard/claim/ghost-row")
    assert r.status_code == 404


def test_claim_one_off_guest_returns_404():
    """One-off guests are hard-deleted when removed from their creator's
    order, so a cookie must never be bound to one."""
    with _client() as bob, _client() as visitor:
        bob.get("/")
        bob.post("/me/name", data={"display_name": "Bob"})
        bob.post(
            "/people",
            data={"display_name": "Guest", "base_id": "latte", "one_off": "1"},
        )

        from sqlmodel import Session, select

        from app.db import get_engine
        from app.models import User

        with Session(get_engine()) as s:
            guest_id = s.exec(
                select(User).where(User.display_name == "Guest")
            ).first().id

        visitor.get("/")
        r = visitor.post(f"/onboard/claim/{guest_id}")
    assert r.status_code == 404


def test_unnamed_visitors_leave_no_rows():
    """Visiting (or logging out and bouncing) must not litter the user table:
    a row is only written once the visitor names themselves."""
    from sqlmodel import Session, select

    from app.db import get_engine
    from app.models import User

    with _client() as client:
        client.get("/")
        client.get("/")
        with Session(get_engine()) as s:
            assert s.exec(select(User)).all() == []

        client.post("/me/name", data={"display_name": "Sam"})
        with Session(get_engine()) as s:
            rows = s.exec(select(User)).all()
            assert [u.display_name for u in rows] == ["Sam"]


def test_init_db_purges_legacy_unnamed_rows():
    """DBs from before the no-persist change carry junk unnamed rows; startup
    sweeps them (and anything hanging off them) while keeping named users."""
    from sqlmodel import Session, select

    from app.db import get_engine, init_db
    from app.models import SavedDrink, User

    with _client() as client:
        client.get("/")
        client.post("/me/name", data={"display_name": "Sam"})
        with Session(get_engine()) as s:
            s.add(User(id="legacy-ghost"))
            s.add(SavedDrink(user_id="legacy-ghost", base_id="latte"))
            s.commit()

        init_db()

        with Session(get_engine()) as s:
            assert [u.display_name for u in s.exec(select(User)).all()] == ["Sam"]
            assert s.get(SavedDrink, "legacy-ghost") is None
