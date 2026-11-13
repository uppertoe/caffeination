"""Sharing an order with a join code: the buyer's screens O1 to O4 in the
join-by-code plan, and the rules behind them (app.sharing)."""

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from sqlmodel import Session, select


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


def _order(owner_name: str):
    from app.db import get_engine
    from app.orders import open_everyday_order

    with Session(get_engine()) as s:
        return open_everyday_order(s, _user_id_by_name(owner_name))


def _code_in(html: str) -> str:
    return re.search(r'class="share-code-text"[^>]*>([A-Z]{3} [A-Z]{3})<', html).group(1)


def _local(dt: datetime) -> str:
    return dt.astimezone(ZoneInfo("Australia/Melbourne")).strftime("%Y-%m-%dT%H:%M")


# ---- codes -----------------------------------------------------------------


def test_codes_are_six_consonants_and_forgiving_to_type():
    from app.sharing import CODE_ALPHABET, _random_code, format_code, normalise_code

    code = _random_code()
    assert len(code) == 6 and set(code) <= set(CODE_ALPHABET)
    assert not set("AEIOULY") & set(CODE_ALPHABET)
    assert format_code("BKTWQR") == "BKT WQR"
    assert normalise_code("bkt wqr") == "BKTWQR"
    assert normalise_code(" BKT-WQR ") == "BKTWQR"
    assert normalise_code("BKTWQ") is None
    assert normalise_code("BKTWQA") is None  # a vowel can't be in a code


def test_a_new_code_skips_one_a_live_order_is_using(monkeypatch):
    from app import sharing
    from app.db import get_engine, init_db
    from app.models import Order, User

    init_db()
    with Session(get_engine()) as s:
        s.add(User(id="u1", display_name="One"))
        s.add(Order(owner_id="u1", code="BBBBBB"))
        s.add(Order(owner_id="u1", kind="advance", code="CCCCCC", status="cancelled"))
        s.commit()
        picks = iter(["BBBBBB", "CCCCCC"])
        monkeypatch.setattr(sharing, "_random_code", lambda: next(picks))
        # BBBBBB is live; CCCCCC belonged to a closed order and is free again.
        assert sharing._new_code(s) == "CCCCCC"


# ---- O1 and O1b: not shared, and the sharing form ---------------------------


def test_the_order_offers_sharing_and_the_form_defaults_to_thirty_minutes():
    with _client() as me:
        _onboard(me, "Me", base_id="latte")
        page = me.get("/")
        assert 'hx-get="/order/share"' in page.text
        form = me.get("/order/share")
    assert "Joins close:" in form.text
    assert 'value="soon" x-model="when" checked' in form.text
    assert 'name="max_drinks"' in form.text and 'value="8"' in form.text


def test_sharing_at_once_gives_a_code_and_a_thirty_minute_window():
    with _client() as me:
        _onboard(me, "Me", base_id="latte")
        before = datetime.now(timezone.utc)
        r = me.post("/order/share", data={"when": "soon", "max_drinks": "8"})
    assert r.status_code == 200
    code = _code_in(r.text)
    assert "Colleagues can add a drink with this code:" in r.text
    assert "<svg" in r.text  # the QR code, inline
    assert "1 of 8 drinks" in r.text  # the owner's own drink
    assert "0 by code" in r.text
    assert "Each colleague chooses where to deliver" in r.text
    order = _order("Me")
    assert order.code == code.replace(" ", "")
    closes = order.joins_close_at.replace(tzinfo=timezone.utc)
    assert timedelta(minutes=29) < closes - before <= timedelta(minutes=31)


def test_the_qr_code_links_to_the_join_page():
    from app.sharing import qr_svg

    with _client() as me:
        _onboard(me, "Me", base_id="latte")
        r = me.post("/order/share", data={"when": "soon"})
    code = _code_in(r.text).replace(" ", "")
    assert str(qr_svg(f"http://testserver/j/{code}")) in r.text


def test_sharing_with_a_chosen_time_name_location_and_cap():
    when = datetime.now(timezone.utc) + timedelta(days=1)
    with _client() as me:
        _onboard(me, "Me", base_id="latte")
        r = me.post(
            "/order/share",
            data={
                "when": "chosen",
                "closes_at": _local(when),
                "name": "  Tuesday   education meeting ",
                "location": "Seminar room 2",
                "max_drinks": "12",
            },
        )
    assert "<h2>Tuesday education meeting</h2>" in r.text
    assert "Seminar room 2" in r.text and "Each colleague chooses where to deliver" not in r.text
    assert "1 of 12 drinks" in r.text
    # Not today, so the day is shown with the time.
    assert re.search(r"Joins close at \d{1,2}:\d{2} [ap]m, \w{3} \d{1,2} \w{3}", r.text)
    order = _order("Me")
    assert order.name == "Tuesday education meeting" and order.max_drinks == 12


def test_a_closing_time_in_the_past_or_too_far_ahead_is_refused():
    now = datetime.now(timezone.utc)
    with _client() as me:
        _onboard(me, "Me", base_id="latte")
        past = me.post(
            "/order/share",
            data={"when": "chosen", "closes_at": _local(now - timedelta(hours=1)), "name": "Kept"},
        )
        far = me.post(
            "/order/share", data={"when": "chosen", "closes_at": _local(now + timedelta(days=8))}
        )
        blank = me.post("/order/share", data={"when": "chosen", "closes_at": ""})
    assert past.status_code == 422
    assert "Please choose a closing time in the future." in past.text
    assert 'value="Kept"' in past.text  # what was typed survives
    assert "The closing time can be at most seven days ahead." in far.text
    assert "Please choose a closing time." in blank.text
    assert _order("Me") is None or _order("Me").code is None


def test_cancel_returns_to_the_share_button():
    with _client() as me:
        _onboard(me, "Me", base_id="latte")
        me.get("/order/share")
        r = me.get("/order/share/panel")
    assert 'class="share-start"' in r.text


# ---- O2 to O4: shared --------------------------------------------------------


def test_closing_and_reopening_joins():
    with _client() as me:
        _onboard(me, "Me", base_id="latte")
        me.post("/order/share", data={"when": "soon"})
        closed = me.post("/order/joins/close")
        assert 'aria-current="step">Locked</li>' in closed.text
        assert "Closed at" in closed.text and "Reopen for 15 minutes" in closed.text
        assert 'class="share-code"' in closed.text
        reopened = me.post("/order/joins/extend")
    assert "Joins close at" in reopened.text
    closes = _order("Me").joins_close_at.replace(tzinfo=timezone.utc)
    assert timedelta(minutes=14) < closes - datetime.now(timezone.utc) <= timedelta(minutes=15)


def test_allowing_fifteen_more_minutes_adds_to_an_open_window():
    with _client() as me:
        _onboard(me, "Me", base_id="latte")
        me.post("/order/share", data={"when": "soon"})
        first = _order("Me").joins_close_at
        me.post("/order/joins/extend")
    assert _order("Me").joins_close_at - first == timedelta(minutes=15)


def test_replacing_the_code_changes_it():
    with _client() as me:
        _onboard(me, "Me", base_id="latte")
        old = _code_in(me.post("/order/share", data={"when": "soon"}).text)
        new = _code_in(me.post("/order/code/replace").text)
    assert old != new


def test_a_full_order_says_one_person_can_carry_it_and_the_cap_can_be_raised():
    with _client() as me:
        _onboard(me, "Me", base_id="latte")
        for n in range(2):
            with _client() as other:
                _onboard(other, f"Colleague {n}", base_id="espresso")
            me.post(f"/order/add/{_user_id_by_name(f'Colleague {n}')}")
        r = me.post("/order/share", data={"when": "soon", "max_drinks": "3"})
        assert "3 of 3 drinks" in r.text
        assert "This order has reached its limit of three drinks." in r.text
        me.post("/order/cap", data={"max_drinks": "8"})
        for n in range(2, 7):
            with _client() as other:
                _onboard(other, f"Colleague {n}", base_id="espresso")
            me.post(f"/order/add/{_user_id_by_name(f'Colleague {n}')}")
        full = me.get("/order/share/panel")
        assert "8 of 8 drinks" in full.text
        assert (
            "This order has eight drinks, which is about as many as one person can carry. "
            "Colleagues cannot add more unless you allow more drinks."
        ) in full.text
        raised = me.post("/order/cap", data={"max_drinks": "9"})
    assert "8 of 9 drinks" in raised.text and "one person can carry" not in raised.text


def test_a_cap_of_one_reads_in_the_singular():
    with _client() as me:
        _onboard(me, "Me", base_id="latte")
        r = me.post("/order/share", data={"when": "soon", "max_drinks": "1"})
    assert "1 of 1 drink<" in r.text
    assert "its limit of one drink." in r.text
    assert "1 drink</span>" in r.text


def test_the_cap_is_kept_within_bounds():
    with _client() as me:
        _onboard(me, "Me", base_id="latte")
        me.post("/order/share", data={"when": "soon"})
        me.post("/order/cap", data={"max_drinks": "0"})
        assert _order("Me").max_drinks == 1
        me.post("/order/cap", data={"max_drinks": "500"})
        assert _order("Me").max_drinks == 40


def test_adding_someone_updates_the_shared_panel_out_of_band():
    with _client() as me, _client() as alice:
        _onboard(alice, "Alice", base_id="latte")
        _onboard(me, "Me", base_id="espresso")
        me.post("/order/share", data={"when": "soon"})
        alice_id = _user_id_by_name("Alice")
        r = me.post(f"/order/add/{alice_id}", headers={"HX-Target": f"roster-{alice_id}"})
    assert 'id="share-panel"' in r.text and 'hx-swap-oob="true"' in r.text
    assert "2 of 8 drinks" in r.text


def test_an_unshared_order_leaves_the_panel_alone_on_add():
    with _client() as me, _client() as alice:
        _onboard(alice, "Alice", base_id="latte")
        _onboard(me, "Me", base_id="espresso")
        alice_id = _user_id_by_name("Alice")
        r = me.post(f"/order/add/{alice_id}", headers={"HX-Target": f"roster-{alice_id}"})
    assert 'id="share-panel"' not in r.text


def test_controls_on_an_unshared_order_change_nothing():
    with _client() as me:
        _onboard(me, "Me", base_id="latte")
        r = me.post("/order/joins/extend")
    assert r.status_code == 200 and 'class="share-start"' in r.text
    assert _order("Me") is None


def test_clearing_a_shared_order_releases_the_code():
    with _client() as me:
        _onboard(me, "Me", base_id="latte")
        me.post("/order/share", data={"when": "soon"})
        me.post("/order/clear")
        r = me.get("/")
    assert 'class="share-start"' in r.text


# ---- expiry ------------------------------------------------------------------


def test_a_shared_order_expires_twelve_hours_after_joins_close():
    from app.models import Order
    from app.orders import _expired

    now = datetime.now(timezone.utc)
    order = Order(owner_id="x", joins_close_at=now + timedelta(days=1))
    # A first drink 13 hours ago would expire a private order...
    assert _expired(Order(owner_id="x"), now - timedelta(hours=13), now)
    # ...but not one whose joins close tomorrow, nor until 12 hours after.
    assert not _expired(order, now - timedelta(hours=13), now)
    assert not _expired(order, None, now + timedelta(days=1, hours=11))
    assert _expired(order, None, now + timedelta(days=1, hours=12))


# ---- times -------------------------------------------------------------------


def test_times_read_as_the_department_reads_them():
    from app.sharing import format_time

    tz = ZoneInfo("Australia/Melbourne")
    now = datetime(2026, 11, 9, 9, 0, tzinfo=tz)
    assert format_time(datetime(2026, 11, 9, 10, 42, tzinfo=tz), now) == "10:42 am"
    assert format_time(datetime(2026, 11, 9, 12, 5, tzinfo=tz), now) == "12:05 pm"
    assert format_time(datetime(2026, 11, 10, 16, 0, tzinfo=tz), now) == "4:00 pm, Tue 10 Nov"
