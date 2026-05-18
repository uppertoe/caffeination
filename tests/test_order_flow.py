from fastapi.testclient import TestClient


def _client() -> TestClient:
    from app.main import create_app

    return TestClient(create_app())


def _user_id_by_name(name: str) -> str:
    from sqlmodel import Session, select

    from app.db import get_engine
    from app.models import User

    with Session(get_engine()) as s:
        return s.exec(select(User).where(User.display_name == name)).first().id


def _onboard(client, name: str, **drink_form):
    client.get("/")
    client.post("/me/name", data={"display_name": name})
    if drink_form:
        client.post("/me/drink", data=drink_form)


def test_roster_lists_other_user_who_has_saved_drink():
    with _client() as alice, _client() as bob:
        _onboard(
            alice,
            "Alice",
            base_id="latte",
            size="regular",
            milk="oat",
        )
        _onboard(bob, "Bob")
        r = bob.get("/")
    assert "Alice" in r.text
    assert "oat latte" in r.text  # bob sees alice's drink in the roster


def test_roster_excludes_users_without_saved_drinks():
    with _client() as alice, _client() as bob:
        _onboard(alice, "Alice")  # no drink saved
        _onboard(bob, "Bob")
        r = bob.get("/")
    assert "Alice" not in r.text  # alice is not in bob's roster


def test_add_then_remove_target_from_order():
    with _client() as alice, _client() as bob:
        _onboard(
            alice,
            "Alice",
            base_id="flat_white",
            size="large",
            milk="oat",
        )
        _onboard(bob, "Bob", base_id="espresso", shots="2")
        alice_id = _user_id_by_name("Alice")

        r = bob.post(f"/order/add/{alice_id}")
        assert r.status_code == 200
        # Bob's order now lists both their drink and Alice's drink.
        assert "double espresso" in r.text
        assert "large oat flat white" in r.text

        r = bob.post(f"/order/remove/{alice_id}")
        assert r.status_code == 200
        # Alice moves back to the roster (Add button) and out of the order.
        # Look for the Add affordance specifically rather than just her name.
        assert 'hx-post="/order/add/' in r.text


def test_saving_own_drink_updates_order_section_via_oob():
    """The drink form POST should also refresh the order section so that
    'you (Alice) — oat latte' appears without a page reload."""
    with _client() as alice:
        alice.get("/")
        alice.post("/me/name", data={"display_name": "Alice"})
        r = alice.post(
            "/me/drink",
            data={"base_id": "latte", "size": "regular", "milk": "oat"},
        )
    assert r.status_code == 200
    # OOB section is appended to the response body.
    assert 'id="order-section"' in r.text
    assert 'hx-swap-oob="true"' in r.text
    assert "Alice" in r.text
    assert "oat latte" in r.text


def test_owner_cant_add_self_to_order_via_add_endpoint():
    with _client() as alice:
        _onboard(alice, "Alice", base_id="latte", size="regular", milk="oat")
        alice_id = _user_id_by_name("Alice")
        r = alice.post(f"/order/add/{alice_id}")
        assert r.status_code == 200
        # Alice still appears once — as (you) — and there should be no Remove button on her row.
        assert r.text.count("Alice") == 1
        assert "(you)" in r.text


def test_adding_unknown_target_is_noop():
    with _client() as alice:
        _onboard(alice, "Alice", base_id="latte", size="regular", milk="oat")
        r = alice.post("/order/add/nonexistent")
        assert r.status_code == 200
        # Still just Alice (the owner row).
        assert r.text.count("Alice") == 1
