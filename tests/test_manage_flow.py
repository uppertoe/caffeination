"""Rename, delete, and clear-order flows (plus the 12-hour order expiry)."""

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient


def _client() -> TestClient:
    from app.main import create_app

    return TestClient(create_app())


def _onboard(client, name: str, **drink_form):
    client.get("/")
    client.post("/me/name", data={"display_name": name})
    if drink_form:
        client.post("/me/drink", data=drink_form)


def _user_id_by_name(name: str):
    from sqlmodel import Session, select

    from app.db import get_engine
    from app.models import User

    with Session(get_engine()) as s:
        row = s.exec(select(User).where(User.display_name == name)).first()
        return row.id if row else None


# ---------------------------------------------------------------------------
# Rename
# ---------------------------------------------------------------------------


def test_rename_updates_header_and_order_row():
    with _client() as client:
        _onboard(client, "Sam", base_id="latte", size="regular", milk="oat")
        r = client.post("/me/rename", data={"display_name": "Samantha"})
        assert r.status_code == 200
        assert "Hi, Samantha" in r.text
        # Own order row refreshes via the order-refresh event.
        assert r.headers["HX-Trigger"] == "order-refresh"
        r = client.get("/order")
        assert "Samantha" in r.text

        r = client.get("/")
        assert "Hi, Samantha" in r.text
        assert "Hi, Sam<" not in r.text


def test_rename_to_taken_name_rejected():
    with _client() as alice, _client() as bob:
        _onboard(alice, "Alice")
        _onboard(bob, "Bob")
        r = bob.post("/me/rename", data={"display_name": "ALICE"})
        assert r.status_code == 409
        assert "already taken" in r.text
        # Bob keeps his name.
        r = bob.get("/")
        assert "Hi, Bob" in r.text


def test_rename_to_own_name_different_case_allowed():
    with _client() as client:
        _onboard(client, "sam")
        r = client.post("/me/rename", data={"display_name": "Sam"})
        assert r.status_code == 200
        assert "Hi, Sam" in r.text


def test_rename_blank_rejected():
    with _client() as client:
        _onboard(client, "Sam")
        r = client.post("/me/rename", data={"display_name": "   "})
        assert r.status_code == 422
        assert "Name must be" in r.text


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


def test_delete_removes_user_drink_and_returns_to_onboarding():
    with _client() as client:
        _onboard(client, "Sam", base_id="latte", size="regular", milk="oat")
        r = client.post("/me/delete")
        assert r.status_code == 200
        assert "Welcome to" in r.text
        assert _user_id_by_name("Sam") is None

        # Fresh visit: Sam isn't claimable any more.
        r = client.get("/")
        assert "Sam" not in r.text


def test_delete_removes_user_from_other_open_orders():
    with _client() as alice, _client() as bob:
        _onboard(alice, "Alice", base_id="latte", size="regular", milk="oat")
        _onboard(bob, "Bob", base_id="espresso")
        alice_id = _user_id_by_name("Alice")
        bob.post(f"/order/add/{alice_id}")

        alice.post("/me/delete")

        r = bob.get("/")
        assert "Alice" not in r.text
        assert "oat latte" not in r.text


def test_delete_keeps_people_they_created():
    with _client() as bob:
        _onboard(bob, "Bob", base_id="espresso")
        bob.post(
            "/people",
            data={"display_name": "Priya", "base_id": "latte", "size": "small", "milk": "oat"},
        )
        bob.post("/me/delete")

    assert _user_id_by_name("Bob") is None
    # Priya stays on the roster, just detached from her creator.
    assert _user_id_by_name("Priya") is not None


def test_delete_cleans_up_own_one_offs():
    with _client() as bob:
        _onboard(bob, "Bob", base_id="espresso")
        bob.post(
            "/people",
            data={"display_name": "Guest", "base_id": "latte", "one_off": "1"},
        )
        bob.post("/me/delete")

    assert _user_id_by_name("Guest") is None


# ---------------------------------------------------------------------------
# Clear order + expiry
# ---------------------------------------------------------------------------


def test_clear_order_empties_order_and_restores_roster():
    with _client() as alice, _client() as bob:
        _onboard(alice, "Alice", base_id="flat_white", size="large", milk="oat")
        _onboard(bob, "Bob", base_id="espresso")
        alice_id = _user_id_by_name("Alice")
        bob.post(f"/order/add/{alice_id}")

        r = bob.post("/order/clear")
        assert r.status_code == 200
        # Alice is back in the roster with an Add button; order shows just Bob.
        assert 'hx-post="/order/add/' in r.text
        assert "large oat flat white" in r.text  # roster line, not order row
        assert f'hx-post="/order/remove/{alice_id}"' not in r.text


def test_clear_order_deletes_one_off_guests():
    with _client() as bob:
        _onboard(bob, "Bob", base_id="espresso")
        bob.post(
            "/people",
            data={"display_name": "Guest", "base_id": "latte", "one_off": "1"},
        )
        assert _user_id_by_name("Guest") is not None
        bob.post("/order/clear")
    assert _user_id_by_name("Guest") is None


def test_order_expires_after_12_hours():
    from sqlmodel import Session, select

    from app.db import get_engine
    from app.models import OrderItem

    with _client() as alice, _client() as bob:
        _onboard(alice, "Alice", base_id="latte", size="regular", milk="oat")
        _onboard(bob, "Bob", base_id="espresso")
        alice_id = _user_id_by_name("Alice")
        bob.post(f"/order/add/{alice_id}")

        # Backdate the item past the TTL.
        with Session(get_engine()) as s:
            item = s.exec(select(OrderItem)).one()
            item.added_at = datetime.now(timezone.utc) - timedelta(hours=13)
            s.add(item)
            s.commit()

        r = bob.get("/")
        assert r.status_code == 200
        # Order cleared: Alice is offered from the roster again.
        assert 'hx-post="/order/add/' in r.text
        assert f'hx-post="/order/remove/{alice_id}"' not in r.text
        with Session(get_engine()) as s:
            assert s.exec(select(OrderItem)).all() == []


def test_fresh_order_survives_a_render():
    with _client() as alice, _client() as bob:
        _onboard(alice, "Alice", base_id="latte", size="regular", milk="oat")
        _onboard(bob, "Bob", base_id="espresso")
        alice_id = _user_id_by_name("Alice")
        bob.post(f"/order/add/{alice_id}")
        r = bob.get("/")
        assert 'hx-post="/order/remove/' in r.text
