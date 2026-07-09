"""Creating a person + their usual on someone else's behalf (POST /people)."""

from fastapi.testclient import TestClient


def _client() -> TestClient:
    from app.main import create_app

    return TestClient(create_app())


def _onboard(client, name: str):
    client.get("/")
    client.post("/me/name", data={"display_name": name})


def test_create_person_adds_them_to_order_and_roster():
    with _client() as bob, _client() as carol:
        _onboard(bob, "Bob")
        r = bob.post(
            "/people",
            data={"display_name": "Priya", "base_id": "latte", "size": "small", "milk": "oat"},
        )
        assert r.status_code == 200
        # The response is the reset slot; the order section refreshes itself
        # via the order-refresh event.
        assert r.headers["HX-Trigger"] == "order-refresh"
        r = bob.get("/order")
        # Priya joins Bob's order immediately, with her saved drink.
        # (small is the default size, so it isn't spelled out.)
        assert "Priya" in r.text
        assert "oat latte" in r.text

        # She's a global citizen now — Carol sees her in the roster.
        _onboard(carol, "Carol")
        r = carol.get("/")
        assert "Priya" in r.text
        assert "oat latte" in r.text


def test_create_person_duplicate_name_rejected():
    with _client() as alice, _client() as bob:
        _onboard(alice, "Alice")
        _onboard(bob, "Bob")
        r = bob.post(
            "/people",
            data={"display_name": "ALICE", "base_id": "latte"},
        )
    assert r.status_code == 409
    assert "already on the list" in r.text


def test_create_person_blank_name_rejected():
    with _client() as bob:
        _onboard(bob, "Bob")
        r = bob.post(
            "/people",
            data={"display_name": "   ", "base_id": "latte"},
        )
    assert r.status_code == 422
    assert "Name must be" in r.text


def test_create_person_honours_drink_rules():
    """An espresso has no milk/size; normalization should strip them so the
    till line is just 'espresso'."""
    with _client() as bob:
        _onboard(bob, "Bob")
        r = bob.post(
            "/people",
            data={"display_name": "Dee", "base_id": "espresso", "size": "large", "milk": "oat"},
        )
        assert r.status_code == 200
        r = bob.get("/order")
    assert "Dee" in r.text
    assert "espresso" in r.text
    assert "oat" not in r.text.split("espresso")[1][:40]
