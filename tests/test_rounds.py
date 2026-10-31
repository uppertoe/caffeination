from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlmodel import Session, select, update


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _client() -> TestClient:
    from app.main import create_app

    return TestClient(create_app())


def _user_id_by_name(name: str) -> str:
    from app.db import get_engine
    from app.models import User

    with Session(get_engine()) as s:
        return s.exec(select(User).where(User.display_name == name)).first().id


def _onboard(client, name: str, **drink_form):
    client.get("/")
    client.post("/me/name", data={"display_name": name})
    if drink_form:
        client.post("/me/drink", data=drink_form)


def _rounds():
    from app.db import get_engine
    from app.models import Round, RoundDrink

    with Session(get_engine()) as s:
        rounds = s.exec(select(Round).order_by(Round.id)).all()
        drinks = s.exec(select(RoundDrink).order_by(RoundDrink.id)).all()
        return [r.model_dump() for r in rounds], [d.model_dump() for d in drinks]


def _age_order(owner_id: str, by: timedelta):
    from app.db import get_engine
    from app.models import OrderItem

    with Session(get_engine()) as s:
        s.exec(
            update(OrderItem)
            .where(OrderItem.owner_id == owner_id)
            .values(added_at=datetime.now(timezone.utc) - by)
        )
        s.commit()


def test_clearing_an_order_logs_its_drinks_linked_to_people():
    with _client() as me, _client() as alice:
        _onboard(alice, "Alice", base_id="latte", size="large", milk="oat", notes="extra hot please")
        _onboard(me, "Me", base_id="espresso")
        me.post(f"/order/add/{_user_id_by_name('Alice')}")
        me.post("/people", data={"display_name": "Guest", "base_id": "piccolo", "one_off": "1"})
        alice_id, me_id = _user_id_by_name("Alice"), _user_id_by_name("Me")
        me.post("/order/clear")

    rounds, drinks = _rounds()
    assert len(rounds) == 1 and rounds[0]["drink_count"] == 3
    assert rounds[0]["owner_id"] == me_id
    by_base = {d["base_id"]: d for d in drinks}
    assert by_base["latte"]["user_id"] == alice_id
    assert by_base["latte"]["milk"] == "oat" and by_base["latte"]["has_notes"] is True
    assert by_base["espresso"]["user_id"] == me_id
    # The guest is deleted with the order, so their drink keeps no link.
    assert by_base["piccolo"]["guest"] is True and by_base["piccolo"]["user_id"] is None
    assert "extra hot" not in repr(drinks)


def test_placed_at_is_the_exact_time_of_the_last_addition():
    from app.users import _as_naive_utc

    with _client() as me, _client() as alice:
        _onboard(alice, "Alice", base_id="latte")
        _onboard(me, "Me", base_id="espresso")
        me.post(f"/order/add/{_user_id_by_name('Alice')}")
        added = _now()
        me.post("/order/clear")
    placed = _as_naive_utc(_rounds()[0][0]["placed_at"])
    assert abs(placed - added) < timedelta(seconds=5)


def test_deleting_yourself_detaches_your_rounds_and_deletes_your_history():
    from app.db import get_engine
    from app.models import DrinkChange

    with _client() as me, _client() as alice:
        _onboard(alice, "Alice", base_id="latte")
        _onboard(me, "Me", base_id="espresso")
        alice_id = _user_id_by_name("Alice")
        me.post(f"/order/add/{alice_id}")
        me.post("/order/clear")
        alice.post("/me/delete")
    rounds, drinks = _rounds()
    assert len(drinks) == 2 and alice_id not in repr(drinks)
    with Session(get_engine()) as s:
        assert s.exec(select(DrinkChange).where(DrinkChange.user_id == alice_id)).all() == []


def test_an_owner_alone_is_not_a_round():
    with _client() as me:
        _onboard(me, "Me", base_id="latte")
        me.post("/order/clear")
    assert _rounds() == ([], [])


def test_an_owner_who_opted_out_is_left_out_of_the_round():
    with _client() as me, _client() as alice:
        _onboard(alice, "Alice", base_id="latte")
        _onboard(me, "Me", base_id="espresso")
        me.post(f"/order/remove/{_user_id_by_name('Me')}")
        me.post(f"/order/add/{_user_id_by_name('Alice')}")
        me.post("/order/clear")
    _, drinks = _rounds()
    assert [d["base_id"] for d in drinks] == ["latte"]


def test_an_order_left_to_expire_is_logged_when_its_owner_returns():
    with _client() as me, _client() as alice:
        _onboard(alice, "Alice", base_id="latte")
        _onboard(me, "Me", base_id="espresso")
        me.post(f"/order/add/{_user_id_by_name('Alice')}")
        _age_order(_user_id_by_name("Me"), timedelta(hours=13))
        me.get("/")
    rounds, _ = _rounds()
    assert len(rounds) == 1 and rounds[0]["drink_count"] == 2
    from app.users import _as_naive_utc

    assert _as_naive_utc(rounds[0]["placed_at"]) < _now() - timedelta(hours=12)


def test_sweep_logs_every_expired_order_but_leaves_fresh_ones():
    from app.db import get_engine
    from app.models import OrderItem
    from app.orders import purge_all_expired_orders

    with _client() as me, _client() as bob, _client() as alice:
        _onboard(alice, "Alice", base_id="latte")
        _onboard(me, "Me", base_id="espresso")
        _onboard(bob, "Bob", base_id="piccolo")
        me.post(f"/order/add/{_user_id_by_name('Alice')}")
        bob.post(f"/order/add/{_user_id_by_name('Alice')}")
        _age_order(_user_id_by_name("Me"), timedelta(hours=13))

    with Session(get_engine()) as s:
        assert purge_all_expired_orders(s) == 1
        owners = {i.owner_id for i in s.exec(select(OrderItem)).all()}
    assert owners == {_user_id_by_name("Bob")}
    assert len(_rounds()[0]) == 1


def test_deleting_yourself_does_not_log_a_round():
    with _client() as me, _client() as alice:
        _onboard(alice, "Alice", base_id="latte")
        _onboard(me, "Me", base_id="espresso")
        me.post(f"/order/add/{_user_id_by_name('Alice')}")
        me.post("/me/delete")
    assert _rounds() == ([], [])
