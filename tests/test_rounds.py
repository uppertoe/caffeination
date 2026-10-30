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


def test_clearing_an_order_logs_its_drinks_without_people():
    with _client() as me, _client() as alice:
        _onboard(alice, "Alice", base_id="latte", size="large", milk="oat", notes="extra hot please")
        _onboard(me, "Me", base_id="espresso")
        me.post(f"/order/add/{_user_id_by_name('Alice')}")
        me.post("/people", data={"display_name": "Guest", "base_id": "piccolo", "one_off": "1"})
        alice_id, me_id = _user_id_by_name("Alice"), _user_id_by_name("Me")
        me.post("/order/clear")

    rounds, drinks = _rounds()
    assert len(rounds) == 1 and rounds[0]["drink_count"] == 3
    assert sorted(d["base_id"] for d in drinks) == ["espresso", "latte", "piccolo"]
    latte = next(d for d in drinks if d["base_id"] == "latte")
    assert latte["milk"] == "oat" and latte["size"] == "large"
    assert latte["has_notes"] is True
    assert [d["guest"] for d in drinks if d["base_id"] == "piccolo"] == [True]
    # Nothing that identifies a person: no ids, names or note text anywhere.
    logged = repr((rounds, drinks))
    for needle in (alice_id, me_id, "Alice", "Me'", "Guest", "extra hot"):
        assert needle not in logged


def test_placed_at_is_the_last_addition_floored_to_fifteen_minutes():
    from app.rounds import floor_placed_at

    utc = timezone.utc
    assert floor_placed_at(datetime(2026, 10, 31, 10, 44, 59)) == datetime(2026, 10, 31, 10, 30, tzinfo=utc)
    assert floor_placed_at(datetime(2026, 10, 31, 10, 45, tzinfo=utc)) == datetime(2026, 10, 31, 10, 45, tzinfo=utc)


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
