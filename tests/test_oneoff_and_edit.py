"""One-off (ephemeral guest) orders and editing people you created."""

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient


def _client() -> TestClient:
    from app.main import create_app

    return TestClient(create_app())


def _onboard(client, name: str):
    client.get("/")
    client.post("/me/name", data={"display_name": name})


def _user_id_by_name(name: str) -> str:
    from sqlmodel import Session, select

    from app.db import get_engine
    from app.models import User

    with Session(get_engine()) as s:
        return s.exec(select(User).where(User.display_name == name)).first().id


def _backdate_user(name: str, hours: float) -> None:
    """Pretend a user was created `hours` ago so the edit window can be tested."""
    from sqlmodel import Session, select

    from app.db import get_engine
    from app.models import User

    with Session(get_engine()) as s:
        u = s.exec(select(User).where(User.display_name == name)).first()
        u.created_at = datetime.now(timezone.utc) - timedelta(hours=hours)
        s.add(u)
        s.commit()


# --------------------------------------------------------------------------- #
# One-off orders
# --------------------------------------------------------------------------- #


def test_one_off_is_in_order_but_not_roster_or_onboarding():
    with _client() as bob, _client() as carol:
        _onboard(bob, "Bob")
        r = bob.post(
            "/people",
            data={"display_name": "Guest", "base_id": "latte", "size": "small",
                  "milk": "oat", "one_off": "1"},
        )
        assert r.status_code == 200
        assert r.headers["HX-Trigger"] == "order-refresh"
        r = bob.get("/order")
        assert "Guest" in r.text  # appears in Bob's order
        assert "oat latte" in r.text
        assert "one-off" in r.text  # tagged

        # Carol does NOT see the guest in onboarding or her roster.
        carol.get("/")
        r = carol.get("/")
        assert "Guest" not in r.text


def test_one_off_allows_duplicate_names():
    with _client() as bob:
        _onboard(bob, "Bob")
        r1 = bob.post("/people", data={"display_name": "Guest", "base_id": "espresso", "one_off": "1"})
        r2 = bob.post("/people", data={"display_name": "Guest", "base_id": "long_black", "one_off": "1"})
        assert r1.status_code == 200 and r2.status_code == 200
        # Two separate guests both named "Guest" in the order.
        assert bob.get("/").text.count("Guest") == 2


def test_one_off_name_can_match_existing_roster_member():
    with _client() as alice, _client() as bob:
        _onboard(alice, "Alice")
        _onboard(bob, "Bob")
        # A roster "Alice" exists; a one-off "Alice" is still allowed.
        r = bob.post("/people", data={"display_name": "Alice", "base_id": "espresso", "one_off": "1"})
        assert r.status_code == 200


def test_removing_one_off_deletes_it():
    from sqlmodel import Session, select

    from app.db import get_engine
    from app.models import User

    with _client() as bob:
        _onboard(bob, "Bob")
        bob.post("/people", data={"display_name": "Guest", "base_id": "espresso", "one_off": "1"})
        guest_id = _user_id_by_name("Guest")
        r = bob.post(f"/order/remove/{guest_id}")
        assert r.status_code == 200
        assert "Guest" not in r.text
        # The throwaway user row is gone, not just the order item.
        with Session(get_engine()) as s:
            assert s.get(User, guest_id) is None


# --------------------------------------------------------------------------- #
# Editing people you created
# --------------------------------------------------------------------------- #


def test_creator_can_edit_roster_person_within_window():
    with _client() as bob:
        _onboard(bob, "Bob")
        bob.post("/people", data={"display_name": "Priya", "base_id": "latte", "size": "small", "milk": "oat"})
        priya_id = _user_id_by_name("Priya")

        # Edit affordance is offered on the order row.
        assert f"/people/{priya_id}/edit" in bob.get("/").text

        # Change Priya's usual to a large flat white.
        r = bob.post(
            f"/people/{priya_id}/drink",
            data={"base_id": "flat_white", "size": "large", "milk": "oat"},
        )
        assert r.status_code == 200
        assert "large oat flat white" in r.text
        assert "oat latte" not in r.text


def test_creator_cannot_edit_roster_person_after_window():
    with _client() as bob:
        _onboard(bob, "Bob")
        bob.post("/people", data={"display_name": "Priya", "base_id": "latte", "size": "small", "milk": "oat"})
        priya_id = _user_id_by_name("Priya")
        _backdate_user("Priya", hours=3)  # older than the 2h window

        # No edit affordance anymore.
        assert f"/people/{priya_id}/edit" not in bob.get("/").text

        # And the endpoint refuses to change anything.
        r = bob.post(
            f"/people/{priya_id}/drink",
            data={"base_id": "flat_white", "size": "large", "milk": "oat"},
        )
        assert r.status_code == 403
        assert "oat latte" in bob.get("/").text  # unchanged


def test_one_off_stays_editable_regardless_of_age():
    with _client() as bob:
        _onboard(bob, "Bob")
        bob.post("/people", data={"display_name": "Guest", "base_id": "latte", "size": "small", "milk": "oat", "one_off": "1"})
        guest_id = _user_id_by_name("Guest")
        _backdate_user("Guest", hours=5)  # way past the window

        r = bob.post(
            f"/people/{guest_id}/drink",
            data={"base_id": "espresso", "notes": "double shot"},
        )
        assert r.status_code == 200
        assert "espresso (double shot)" in r.text


def test_non_creator_cannot_edit():
    with _client() as bob, _client() as carol:
        _onboard(bob, "Bob")
        _onboard(carol, "Carol")
        bob.post("/people", data={"display_name": "Priya", "base_id": "latte", "size": "small", "milk": "oat"})
        priya_id = _user_id_by_name("Priya")

        # Carol didn't create Priya — she gets no edit affordance and is refused.
        assert f"/people/{priya_id}/edit" not in carol.get("/").text
        r = carol.post(
            f"/people/{priya_id}/drink",
            data={"base_id": "flat_white", "size": "large", "milk": "oat"},
        )
        assert r.status_code == 403
