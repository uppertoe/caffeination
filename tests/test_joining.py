"""Joining an order by code: the joiner's screens J0 to J18 in the
join-by-code plan, and what the buyer sees of them (app.joining,
app.join_page)."""

import html
import re

from fastapi.testclient import TestClient
from sqlmodel import Session, select


def T(response) -> str:
    """The page text with entities decoded ("Sarah's", not "Sarah&#39;s")."""
    return html.unescape(response.text)


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


def _share(buyer, **form) -> str:
    r = buyer.post("/order/share", data={"when": "soon", **form})
    return re.search(r'class="share-code-text"[^>]*>([A-Z]{3}) ([A-Z]{3})<', T(r)).group(0)[-8:-1].replace(" ", "")


def _items():
    from app.db import get_engine
    from app.models import OrderItem

    with Session(get_engine()) as s:
        return s.exec(select(OrderItem)).all()


# ---- J0 to J2: typing a code ------------------------------------------------


def test_the_join_page_asks_for_a_code():
    with _client() as c:
        r = c.get("/join")
    assert r.status_code == 200
    assert "Enter the code from the person buying:" in T(r)
    assert '<a href="/" class="back-link">Back to the home page</a>' in r.text


def test_a_typed_code_in_any_case_goes_to_the_order():
    with _client() as sarah, _client() as c:
        _onboard(sarah, "Sarah", base_id="latte")
        code = _share(sarah)
        r = c.get(f"/join?code={code[:3].lower()} {code[3:].lower()}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/j/{code}"


def test_an_unknown_code_says_so():
    with _client() as c:
        r = c.get("/join?code=bktwqv")
    assert r.status_code == 404
    assert "No open order uses the code BKT WQV. Please double-check the code with the person buying." in T(r)


HX = {"HX-Request": "true"}


def test_the_home_page_opens_the_join_form_in_a_dialog():
    with _client() as sarah:
        _onboard(sarah, "Sarah", base_id="latte")
        home = sarah.get("/")
        r = sarah.get("/join?dialog=1", headers=HX)
    assert 'hx-get="/join?dialog=1" hx-target="#join-dialog"' in home.text
    assert '<div id="join-dialog"></div>' in home.text
    assert "<dialog open" in r.text
    assert "Enter the code from the person buying:" in T(r) and 'name="dialog" value="1"' in r.text
    assert "<html" not in r.text


def test_a_code_typed_in_the_dialog_goes_to_the_order():
    with _client() as sarah, _client() as c:
        _onboard(sarah, "Sarah", base_id="latte")
        code = _share(sarah)
        r = c.get(f"/join?dialog=1&code={code.lower()}", headers=HX)
    assert r.status_code == 200 and r.headers["HX-Redirect"] == f"/j/{code}"


def test_an_unknown_code_in_the_dialog_says_so_in_the_dialog():
    with _client() as c:
        r = c.get("/join?dialog=1&code=bktwqv", headers=HX)
    assert r.status_code == 404 and "<dialog open" in r.text
    assert "No open order uses the code BKT WQV. Please double-check the code with the person buying." in T(r)


def test_the_dialog_flag_without_htmx_still_gives_the_page():
    with _client() as c:
        r = c.get("/join?dialog=1")
    assert "<html" in r.text and "<dialog" not in r.text


def test_too_many_unknown_codes_are_refused_for_a_minute():
    with _client() as c:
        for _ in range(10):
            c.get("/j/BBBBBB")
        r = c.get("/j/CCCCCC")
    assert r.status_code == 429
    assert "This device has tried too many codes. You can try again in a minute." in T(r)


# ---- J3, J3b, J4, J4b: adding a drink --------------------------------------


def test_someone_with_a_usual_joins_in_one_tap():
    with _client() as sarah, _client() as priya:
        _onboard(sarah, "Sarah", base_id="flat_white", milk="oat")
        _onboard(priya, "Priya", base_id="latte", milk="oat")
        code = _share(sarah)
        landing = priya.get(f"/j/{code}")
        assert "<h2>Sarah's order</h2>" in T(landing)
        assert "Sarah is taking orders until" in T(landing)
        assert "Add my usual drink" in T(landing) and "oat latte" in T(landing)
        assert "If you leave the location blank, you can collect your drink from Sarah." in T(landing)
        joined = priya.post(f"/j/{code}/usual", data={"location": "Theatre 3"})
        assert "Your drink is in Sarah's order." in T(joined)
        assert "Theatre 3" in T(joined) and "2 of 8 drinks" in T(joined)
        # The location is remembered for next time, and suggested to others.
        again = priya.get(f"/j/{code}")
    assert "Your drink is in Sarah's order." in T(again)
    item = _items()[0]
    assert item.joined_by == item.target_user_id == _user_id_by_name("Priya")
    assert item.location == "Theatre 3" and item.base_id is None


def test_a_one_off_drink_leaves_the_usual_alone_and_reaches_the_till():
    with _client() as sarah, _client() as priya:
        _onboard(sarah, "Sarah", base_id="flat_white", milk="oat")
        _onboard(priya, "Priya", base_id="latte", size="small")
        code = _share(sarah)
        form = priya.get(f"/j/{code}?mode=oneoff")
        assert "Save this as my usual drink" in T(form)
        assert "Add my usual drink instead" in T(form)
        r = priya.post(f"/j/{code}/one-off", data={"base_id": "long_black", "mode": "oneoff"})
        assert "One-off: long black" in T(r)
        buyer = sarah.get("/")
    till = T(buyer).split('id="till-summary"')[1].split("</textarea>")[0]
    assert "latte" not in till
    assert "1x long black" in T(buyer)
    from app.db import get_engine
    from app.models import SavedDrink

    with Session(get_engine()) as s:
        assert s.get(SavedDrink, _user_id_by_name("Priya")).base_id == "latte"


def test_a_one_off_can_be_saved_as_the_usual():
    with _client() as sarah, _client() as priya:
        _onboard(sarah, "Sarah", base_id="flat_white")
        _onboard(priya, "Priya", base_id="latte")
        code = _share(sarah)
        priya.post(f"/j/{code}/one-off", data={"base_id": "piccolo", "save_usual": "1"})
    from app.db import get_engine
    from app.models import SavedDrink

    with Session(get_engine()) as s:
        assert s.get(SavedDrink, _user_id_by_name("Priya")).base_id == "piccolo"
    assert _items()[0].base_id is None  # the line now follows the usual


def test_someone_with_no_usual_is_offered_the_builder_saving_by_default():
    with _client() as sarah, _client() as ben:
        _onboard(sarah, "Sarah", base_id="flat_white")
        _onboard(ben, "Ben")
        code = _share(sarah)
        r = ben.get(f"/j/{code}")
    assert 'name="save_usual" value="1" checked' in T(r)
    assert "Add my drink" in T(r) and "Your name:" not in T(r)


def test_a_guest_joins_with_a_name_and_a_drink():
    with _client() as sarah, _client() as tom:
        _onboard(sarah, "Sarah", base_id="flat_white")
        code = _share(sarah)
        landing = tom.get(f"/j/{code}")
        assert "Your name:" in T(landing) and "for the cup" not in T(landing)
        missing = tom.post(f"/j/{code}/guest", data={"display_name": " ", "base_id": "latte"})
        assert missing.status_code == 422 and "Please enter your name." in T(missing)
        r = tom.post(f"/j/{code}/guest", data={"display_name": "Tom", "base_id": "long_black"})
        assert "Your drink is in Sarah's order." in T(r)
        buyer = sarah.get("/")
    assert "Tom" in T(buyer) and 'class="via-code"' in T(buyer)
    # A guest never reaches the roster.
    with _client() as other:
        _onboard(other, "Other")
        assert "Tom" not in other.get("/").text


def test_a_guests_first_drink_is_their_own_and_the_next_a_colleagues():
    with _client() as sarah, _client() as tom:
        _onboard(sarah, "Sarah", base_id="flat_white")
        code = _share(sarah)
        tom.post(f"/j/{code}/guest", data={"display_name": "Tom", "base_id": "latte"})
        mine = tom.get(f"/j/{code}")
        assert "Remove my drink" in T(mine) and "Tom (guest)" not in T(mine)
        both = tom.post(f"/j/{code}/guest", data={"display_name": "Ben", "base_id": "piccolo", "mode": "colleague"})
    assert "Remove my drink" in T(both) and "Remove Ben's drink" in T(both)
    assert "Ben (guest)" in T(both)


# ---- J5, J6, J7: arriving too late -----------------------------------------


def test_closed_full_and_finished_orders_turn_joiners_away_politely():
    with _client() as sarah, _client() as priya:
        _onboard(sarah, "Sarah", base_id="flat_white")
        _onboard(priya, "Priya", base_id="latte")
        code = _share(sarah, max_drinks="1")
        full = priya.get(f"/j/{code}")
        assert "Sarah's order has reached its limit of one drink. If you still need a coffee, you can start your own order." in T(full)
        refused = priya.post(f"/j/{code}/usual")
        assert refused.status_code == 409 and "reached its limit" in T(refused)
        sarah.post("/order/cap", data={"max_drinks": "8"})
        sarah.post("/order/joins/close")
        closed = priya.get(f"/j/{code}")
        assert "Sarah's order is closed to new drinks. If you still need a coffee, you can start your own order." in T(closed)
        sarah.post("/order/clear")
        finished = priya.get(f"/j/{code}")
    # Priya never joined, and join links are public: a finished order
    # answers only devices that joined it.
    assert finished.status_code == 404
    assert f"No open order uses the code {code[:3]} {code[3:]}." in T(finished)


def test_a_full_order_at_the_default_cap_mentions_carrying():
    with _client() as sarah, _client() as late:
        _onboard(sarah, "Sarah", base_id="flat_white")
        code = _share(sarah)
        for n in range(7):
            with _client() as g:
                g.post(f"/j/{code}/guest", data={"display_name": f"G{n}", "base_id": "latte"})
        r = late.get(f"/j/{code}")
    assert "Sarah's order already has eight drinks, which is about as many as one person can carry." in T(r)


# ---- J8, J9, J10: in the order ---------------------------------------------


def test_a_joiner_can_add_one_drink_for_a_colleague_and_no_more():
    with _client() as sarah, _client() as priya:
        _onboard(sarah, "Sarah", base_id="flat_white")
        _onboard(priya, "Priya", base_id="latte")
        code = _share(sarah)
        priya.post(f"/j/{code}/usual")
        form = priya.get(f"/j/{code}?mode=colleague")
        assert "Your colleague's name:" in T(form) and "Add your colleague's drink" in T(form)
        assert "If you leave the location blank, your colleague can collect the drink from Sarah." in T(form)
        r = priya.post(f"/j/{code}/guest", data={"display_name": "Ben", "base_id": "piccolo", "mode": "colleague"})
        assert "Your drinks are in Sarah's order." in T(r)
        assert "Ben (guest)" in T(r) and "Each person can add up to two drinks." in T(r)
        assert "Add a drink for a colleague" not in T(r)
        third = priya.post(f"/j/{code}/guest", data={"display_name": "Cy", "base_id": "latte", "mode": "colleague"})
    assert third.status_code == 409 and "Each person can add up to two drinks." in T(third)
    assert len(_items()) == 2


def test_a_joiner_can_change_their_location_and_remove_their_drink():
    with _client() as sarah, _client() as tom:
        _onboard(sarah, "Sarah", base_id="flat_white")
        code = _share(sarah)
        tom.post(f"/j/{code}/guest", data={"display_name": "Tom", "base_id": "latte"})
        tom_id = _user_id_by_name("Tom")
        edit = tom.get(f"/j/{code}?mode=location&line={tom_id}")
        assert "Save location" in T(edit)
        moved = tom.post(f"/j/{code}/line/{tom_id}/location", data={"location": "Tearoom"})
        assert "Tearoom" in T(moved)
        removed = tom.post(f"/j/{code}/line/{tom_id}/remove")
    # Removing their own drink returns them to the landing page, not "removed".
    assert "Your name:" in T(removed) and "has removed your drink" not in T(removed)
    assert _items() == []


def test_after_joins_close_the_drink_stays_and_can_still_be_removed():
    with _client() as sarah, _client() as priya:
        _onboard(sarah, "Sarah", base_id="flat_white")
        _onboard(priya, "Priya", base_id="latte")
        code = _share(sarah)
        priya.post(f"/j/{code}/usual")
        sarah.post("/order/joins/close")
        r = priya.get(f"/j/{code}")
    assert "Sarah's order is closed to new drinks. Your drink is still in the order." in T(r)
    assert "Remove my drink" in T(r) and "Add a drink for a colleague" not in T(r)


def test_someone_the_buyer_already_added_is_told_so():
    with _client() as sarah, _client() as priya:
        _onboard(priya, "Priya", base_id="latte")
        _onboard(sarah, "Sarah", base_id="flat_white")
        sarah.post(f"/order/add/{_user_id_by_name('Priya')}")
        code = _share(sarah)
        r = priya.get(f"/j/{code}")
        assert "Sarah has already added your usual drink to the order." in T(r)
        assert "Add a location" in T(r) and "Change to a one-off drink" in T(r)
        priya.post(f"/j/{code}/one-off", data={"base_id": "piccolo", "mode": "change"})
    assert _items()[0].base_id == "piccolo" and len(_items()) == 1


# ---- J14, J15, J17 ------------------------------------------------------------


def test_a_joiner_removed_by_the_buyer_is_told():
    with _client() as sarah, _client() as tom:
        _onboard(sarah, "Sarah", base_id="flat_white")
        code = _share(sarah)
        tom.post(f"/j/{code}/guest", data={"display_name": "Tom", "base_id": "latte"})
        sarah.post(f"/order/remove/{_user_id_by_name('Tom')}")
        r = tom.get(f"/j/{code}")
    assert "Sarah has removed your drink from the order. If you still need a coffee, you can speak to Sarah or start your own order." in T(r)


def test_a_joiner_is_told_when_the_buyer_cancels():
    with _client() as sarah, _client() as tom:
        _onboard(sarah, "Sarah", base_id="flat_white")
        code = _share(sarah)
        tom.post(f"/j/{code}/guest", data={"display_name": "Tom", "base_id": "latte"})
        sarah.post("/order/clear")
        r = tom.get(f"/j/{code}")
    assert "Sarah has cancelled the order. Your drink has not been ordered." in T(r)


def test_the_buyer_scanning_their_own_code_goes_home():
    with _client() as sarah:
        _onboard(sarah, "Sarah", base_id="flat_white")
        code = _share(sarah)
        r = sarah.get(f"/j/{code}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"


def test_a_forged_join_cookie_grants_nothing():
    with _client() as sarah, _client() as tom, _client() as mallory:
        _onboard(sarah, "Sarah", base_id="flat_white")
        code = _share(sarah)
        tom.post(f"/j/{code}/guest", data={"display_name": "Tom", "base_id": "latte"})
        tom_id = _user_id_by_name("Tom")
        mallory.cookies.set("join", "not-a-signed-value")
        mallory.post(f"/j/{code}/line/{tom_id}/remove")
    assert len(_items()) == 1


# ---- the buyer's side ----------------------------------------------------------


def test_the_buyer_sees_drinks_grouped_by_location():
    with _client() as sarah, _client() as a, _client() as b:
        _onboard(sarah, "Sarah", base_id="flat_white")
        code = _share(sarah)
        a.post(f"/j/{code}/guest", data={"display_name": "Ann", "base_id": "latte", "location": "Theatre 3"})
        b.post(f"/j/{code}/guest", data={"display_name": "Bo", "base_id": "latte"})
        r = sarah.get("/")
    listing = T(r).split('id="order-list"')[1].split("</article>")[0]
    assert (
        listing.index("Theatre 3")
        < listing.index("<strong>Ann</strong>")
        < listing.index("Collected from you")
        < listing.index("<strong>Bo</strong>")
    )
    assert "2 by code" in T(r)


def test_an_order_wide_location_replaces_the_field():
    with _client() as sarah, _client() as tom:
        _onboard(sarah, "Sarah", base_id="flat_white")
        code = _share(sarah, location="Seminar room 2", name="Tuesday education meeting")
        r = tom.get(f"/j/{code}")
        assert "Tuesday education meeting" in T(r) and "Seminar room 2" in T(r)
        assert 'name="location"' not in T(r)
        tom.post(f"/j/{code}/guest", data={"display_name": "Tom", "base_id": "latte", "location": "Elsewhere"})
    assert _items()[0].location is None


def test_locations_are_suggested_by_use_without_any_link_to_people():
    from app.db import get_engine
    from app.models import PlaceName

    with _client() as sarah:
        _onboard(sarah, "Sarah", base_id="flat_white")
        code = _share(sarah)
        for n, place in enumerate(["Tearoom", "tearoom", "PACU"]):
            with _client() as g:
                g.post(f"/j/{code}/guest", data={"display_name": f"G{n}", "base_id": "latte", "location": place})
        with _client() as late:
            r = late.get(f"/j/{code}")
    assert T(r).index(">tearoom<") < T(r).index(">PACU<")
    with Session(get_engine()) as s:
        places = {p.key: p.uses for p in s.exec(select(PlaceName)).all()}
    assert places == {"tearoom": 2, "pacu": 1}
    assert set(PlaceName.model_fields) == {"key", "name", "uses", "last_used_at"}


def test_clearing_the_order_logs_one_off_drinks_and_deletes_guests():
    from app.db import get_engine
    from app.models import RoundDrink

    with _client() as sarah, _client() as priya, _client() as tom:
        _onboard(sarah, "Sarah", base_id="flat_white")
        _onboard(priya, "Priya", base_id="latte")
        code = _share(sarah)
        priya.post(f"/j/{code}/one-off", data={"base_id": "piccolo"})
        tom.post(f"/j/{code}/guest", data={"display_name": "Tom", "base_id": "long_black"})
        sarah.post("/order/clear")
    with Session(get_engine()) as s:
        drinks = sorted(d.base_id for d in s.exec(select(RoundDrink)).all())
    assert drinks == ["flat_white", "long_black", "piccolo"]
    with _client() as other:
        _onboard(other, "Other")
    from app.models import User

    with Session(get_engine()) as s:
        assert s.exec(select(User).where(User.display_name == "Tom")).first() is None


def test_the_join_page_offers_a_retry_message_when_offline():
    with _client() as sarah, _client() as priya:
        _onboard(sarah, "Sarah", base_id="flat_white")
        _onboard(priya, "Priya", base_id="latte")
        code = _share(sarah)
        r = priya.get(f"/j/{code}")
    assert "Your drink has not been added, because the server could not be reached. Please try again." in T(r)
    assert '@htmx:send-error="offline = true"' in T(r)


# ---- live updates (phase 4) -------------------------------------------------


def _poll_url(page_text: str) -> str:
    return re.search(r'hx-get="([^"]*\?v=[0-9a-f]+)"', page_text).group(1)


def test_the_buyers_poll_swaps_nothing_until_someone_joins():
    with _client() as sarah, _client() as tom:
        _onboard(sarah, "Sarah", base_id="flat_white")
        code = _share(sarah)
        page = T(sarah.get("/"))
        url = _poll_url(page)
        assert url.startswith("/order/live?v=")
        assert sarah.get(url).status_code == 204
        tom.post(f"/j/{code}/guest", data={"display_name": "Tom", "base_id": "long_black"})
        changed = sarah.get(url)
    assert changed.status_code == 200
    assert 'id="order-list" hx-swap-oob="true"' in T(changed)
    assert "<strong>Tom</strong>" in T(changed) and "1 by code" in T(changed)
    assert 'id="roster-picker"' not in T(changed)


def test_the_buyers_poll_notices_joins_closing_on_the_clock():
    from app.db import get_engine
    from app.models import Order

    with _client() as sarah:
        _onboard(sarah, "Sarah", base_id="flat_white")
        _share(sarah)
        url = _poll_url(T(sarah.get("/")))
        with Session(get_engine()) as s:
            order = s.exec(select(Order)).one()
            from datetime import datetime, timedelta, timezone

            order.joins_close_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            s.add(order)
            s.commit()
        r = sarah.get(url)
    assert r.status_code == 200 and "This order is closed to new drinks." in T(r)


def test_an_unshared_order_is_not_polled():
    with _client() as sarah:
        _onboard(sarah, "Sarah", base_id="flat_white")
        assert "/order/live" not in T(sarah.get("/"))
        _share(sarah)
        sarah.post("/order/clear")
        assert "/order/live" not in T(sarah.get("/"))


def test_the_joiners_page_follows_the_buyer():
    with _client() as sarah, _client() as tom:
        _onboard(sarah, "Sarah", base_id="flat_white")
        code = _share(sarah)
        joined = T(tom.post(f"/j/{code}/guest", data={"display_name": "Tom", "base_id": "latte"}))
        url = _poll_url(joined)
        assert tom.get(url, headers={"HX-Request": "true"}).status_code == 204
        sarah.post("/order/joins/close")
        closed = tom.get(url, headers={"HX-Request": "true"})
        assert "Sarah's order is closed to new drinks. Your drink is still in the order." in T(closed)
        assert "<html" not in T(closed)  # just the body, for the swap
        sarah.post(f"/order/remove/{_user_id_by_name('Tom')}")
        removed = tom.get(_poll_url(T(closed)), headers={"HX-Request": "true"})
    assert "Sarah has removed your drink from the order." in T(removed)
    assert f"/j/{code}?v=" not in T(removed)  # nothing more to wait for


def test_a_joiner_typing_is_never_polled_over():
    with _client() as sarah, _client() as tom:
        _onboard(sarah, "Sarah", base_id="flat_white")
        code = _share(sarah)
        # The landing page is polled, but swapped only when the form could
        # no longer be accepted, never because someone else joined.
        landing = T(tom.get(f"/j/{code}"))
        url = _poll_url(landing)
        with _client() as ann:
            ann.post(f"/j/{code}/guest", data={"display_name": "Ann", "base_id": "latte"})
        assert tom.get(url, headers={"HX-Request": "true"}).status_code == 204
        sarah.post("/order/joins/close")
        closed = T(tom.get(url, headers={"HX-Request": "true"}))
        assert "Sarah's order is closed to new drinks." in closed and "Your name:" not in closed
        sarah.post("/order/joins/extend")
        tom.post(f"/j/{code}/guest", data={"display_name": "Tom", "base_id": "latte"})
        colleague = T(tom.get(f"/j/{code}?mode=colleague"))
    assert f"/j/{code}?v=" not in colleague


def test_a_joiner_on_a_full_order_sees_it_reopen():
    with _client() as sarah, _client() as tom, _client() as late:
        _onboard(sarah, "Sarah", base_id="flat_white")
        code = _share(sarah, max_drinks="2")
        tom.post(f"/j/{code}/guest", data={"display_name": "Tom", "base_id": "latte"})
        full = T(late.get(f"/j/{code}"))
        assert "reached its limit of two drinks" in full
        sarah.post(f"/order/remove/{_user_id_by_name('Tom')}")
        reopened = late.get(_poll_url(full), headers={"HX-Request": "true"})
    assert "Your name:" in T(reopened)


def test_clearing_an_order_people_joined_asks_first():
    with _client() as sarah, _client() as a, _client() as b:
        _onboard(sarah, "Sarah", base_id="flat_white")
        _onboard(b, "Bea", base_id="latte")
        sarah.post(f"/order/add/{_user_id_by_name('Bea')}")
        plain = T(sarah.get("/"))
        assert "Keep the order" not in plain  # nobody joined by code yet
        code = _share(sarah)
        a.post(f"/j/{code}/guest", data={"display_name": "Ann", "base_id": "latte"})
        one = T(sarah.get("/"))
        assert "One colleague has joined this order. If you clear it, they will see that the order has been cancelled." in one
        assert "Clear the order" in one and "Keep the order" in one
        with _client() as c:
            c.post(f"/j/{code}/guest", data={"display_name": "Cy", "base_id": "latte"})
        two = T(sarah.get("/"))
    assert "Two colleagues have joined this order." in two


# ---- O5, O7, J11 to J13: the buyer's updates --------------------------------


def _set_closed_at(hours_ago: float):
    from datetime import datetime, timedelta, timezone

    from app.db import get_engine
    from app.models import Order

    with Session(get_engine()) as s:
        order = s.exec(select(Order).where(Order.closed_at != None)).one()  # noqa: E711
        order.closed_at = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
        s.add(order)
        s.commit()


def test_the_update_form_appears_once_someone_joins_by_code():
    with _client() as sarah, _client() as tom:
        _onboard(sarah, "Sarah", base_id="flat_white")
        code = _share(sarah)
        assert "Send an update to everyone who joined:" not in T(sarah.get("/"))
        tom.post(f"/j/{code}/guest", data={"display_name": "Tom", "base_id": "latte"})
        page = T(sarah.get("/"))
    assert "Send an update to everyone who joined:" in page
    assert "Marking the order as ready also completes it." in page


def test_ordered_closes_joins_locks_lines_and_reaches_the_joiner():
    with _client() as sarah, _client() as tom, _client() as late:
        _onboard(sarah, "Sarah", base_id="flat_white")
        code = _share(sarah)
        joined = T(tom.post(f"/j/{code}/guest", data={"display_name": "Tom", "base_id": "latte"}))
        url = _poll_url(joined)
        r = sarah.post("/order/status", data={"status": "ordered", "message": "About ten minutes, there's a queue."})
        panel = T(r)
        assert "Ordered at" in panel and "Your message:" in panel
        assert "About ten minutes, there's a queue." in panel
        assert 'class="share-code-text"' not in panel  # no more joins
        assert "<strong>Tom</strong>" in panel  # the order is still the buyer's
        # J11 on the joiner's next poll, with the tab title.
        polled = T(tom.get(url, headers={"HX-Request": "true"}))
        assert "<title>Ordered · Sarah's order</title>" in polled
        assert "Sarah has ordered your drink." in polled
        assert "Message from Sarah:" in polled and "About ten minutes" in polled
        assert "Remove my drink" not in polled and "Change location" not in polled
        # Lines are locked, and newcomers are turned away.
        tom_id = _user_id_by_name("Tom")
        tom.post(f"/j/{code}/line/{tom_id}/remove")
        assert len(_items()) == 1
        assert "closed to new drinks" in T(late.get(f"/j/{code}"))
        # On the way replaces the message; going back to Ordered is refused.
        sarah.post("/order/status", data={"status": "on_the_way"})
        sarah.post("/order/status", data={"status": "ordered", "message": "Back?"})
        on_way = T(tom.get(f"/j/{code}"))
    assert "Sarah is bringing the drinks back." in on_way
    assert "<title>On the way · Sarah's order</title>" in on_way
    assert "Message from Sarah:" not in on_way and "Back?" not in on_way


def test_ready_completes_the_order_records_the_round_and_tells_joiners():
    from app.db import get_engine
    from app.models import Round, User

    with _client() as sarah, _client() as tom:
        _onboard(sarah, "Sarah", base_id="flat_white")
        code = _share(sarah)
        tom.post(f"/j/{code}/guest", data={"display_name": "Tom", "base_id": "latte", "location": "Theatre 3"})
        r = T(sarah.post("/order/status", data={"status": "ready", "message": "On the bench in the tearoom."}))
        assert "This order is complete. Colleagues who joined have been told that it is ready." in r
        assert "<strong>Tom</strong>" not in r  # the buyer's order is empty again
        ready = T(tom.get(f"/j/{code}"))
        assert "<title>Ready · Sarah's order</title>" in ready
        assert "Your drink is ready." in ready and "On the bench in the tearoom." in ready
        assert "Theatre 3" in ready and "latte" in ready
        assert f"/j/{code}?v=" not in ready  # polling stops here
        with Session(get_engine()) as s:
            rnd = s.exec(select(Round)).one()
            assert rnd.drink_count == 2
            assert s.exec(select(User).where(User.display_name == "Tom")).first() is not None
        # After an hour the joiner is told the order has finished (J7)...
        _set_closed_at(1.5)
        later = T(tom.get(f"/j/{code}"))
        assert "Sarah's order has finished." in later
        # ...and anyone else trying the code is told no open order uses it.
        with _client() as stranger:
            assert stranger.get(f"/j/{code}").status_code == 404


def test_ready_orders_lines_and_guests_are_pruned_with_the_order():
    from datetime import datetime, timedelta, timezone

    from app.db import get_engine
    from app.models import Order, OrderItem, User
    from app.orders import purge_all_expired_orders

    with _client() as sarah, _client() as tom, _client() as amy:
        _onboard(sarah, "Sarah", base_id="flat_white")
        code = _share(sarah)
        tom.post(f"/j/{code}/guest", data={"display_name": "Tom", "base_id": "latte"})
        sarah.post("/order/status", data={"status": "ready"})
        _set_closed_at(30)
        # Pruning rides along with an expiry, so give it one.
        _onboard(amy, "Amy", base_id="latte")
        amy.post(f"/order/add/{_user_id_by_name('Sarah')}")
    with Session(get_engine()) as s:
        for item in s.exec(select(OrderItem)).all():
            item.added_at = datetime.now(timezone.utc) - timedelta(days=2)
            s.add(item)
        s.commit()
        assert purge_all_expired_orders(s) == 1
        assert s.exec(select(User).where(User.display_name == "Tom")).first() is None
        assert s.exec(select(OrderItem)).all() == []
        assert [o.status for o in s.exec(select(Order)).all()] == ["expired"]


def test_a_cancelled_order_is_explained_for_an_hour_then_finished():
    with _client() as sarah, _client() as tom:
        _onboard(sarah, "Sarah", base_id="flat_white")
        code = _share(sarah)
        tom.post(f"/j/{code}/guest", data={"display_name": "Tom", "base_id": "latte"})
        sarah.post("/order/clear")
        assert "<title>Cancelled · Sarah's order</title>" in T(tom.get(f"/j/{code}"))
        _set_closed_at(2)
        r = T(tom.get(f"/j/{code}"))
    assert "Sarah's order has finished." in r


def test_a_finished_order_can_be_shared_again_from_scratch():
    with _client() as sarah, _client() as tom:
        _onboard(sarah, "Sarah", base_id="flat_white")
        code = _share(sarah)
        tom.post(f"/j/{code}/guest", data={"display_name": "Tom", "base_id": "latte"})
        sarah.post("/order/status", data={"status": "ready"})
        panel = T(sarah.get("/order/share/panel"))
        assert "Share this order" in panel
        new_code = _share(sarah)
        r = tom.get(f"/j/{new_code}")
    assert "Sarah is taking orders until" in T(r)  # a fresh order, not the finished one


# ---- Fixes from the review ----------------------------------------------------


def test_replacing_the_code_keeps_joiners_on_their_page():
    with _client() as sarah, _client() as tom, _client() as late:
        _onboard(sarah, "Sarah", base_id="flat_white")
        code = _share(sarah)
        url = _poll_url(T(tom.post(f"/j/{code}/guest", data={"display_name": "Tom", "base_id": "latte"})))
        sarah.post("/order/code/replace")
        polled = tom.get(url, headers={"HX-Request": "true"})
        assert polled.status_code == 200 and "Your drink is in Sarah's order." in T(polled)
        assert f"/j/{code}?v=" not in T(polled)  # it now follows the new code
        assert late.get(f"/j/{code}").status_code == 404  # newcomers need the new code


def test_someone_elses_mistyped_codes_never_block_a_joiner():
    with _client() as sarah, _client() as tom, _client() as typo:
        _onboard(sarah, "Sarah", base_id="flat_white")
        code = _share(sarah)
        tom.post(f"/j/{code}/guest", data={"display_name": "Tom", "base_id": "latte"})
        for _ in range(12):
            typo.get("/j/BBBBBB")  # the same address, as on the hospital network
        assert typo.get(f"/j/{code}").status_code == 429
        assert tom.get(f"/j/{code}").status_code == 200


def test_a_guests_colleague_stays_the_colleagues_after_the_guest_leaves():
    with _client() as sarah, _client() as tom:
        _onboard(sarah, "Sarah", base_id="flat_white")
        code = _share(sarah)
        tom.post(f"/j/{code}/guest", data={"display_name": "Tom", "base_id": "latte"})
        tom.post(f"/j/{code}/guest", data={"display_name": "Ben", "base_id": "piccolo", "mode": "colleague"})
        tom.post(f"/j/{code}/line/{_user_id_by_name('Tom')}/remove")
        r = T(tom.get(f"/j/{code}"))
    assert "Remove Ben's drink" in r and "Remove my drink" not in r


def test_a_usual_drink_is_fixed_when_the_buyer_orders():
    from app.db import get_engine
    from app.models import RoundDrink

    with _client() as sarah, _client() as ben:
        _onboard(sarah, "Sarah", base_id="flat_white")
        _onboard(ben, "Ben", base_id="flat_white")
        code = _share(sarah)
        ben.post(f"/j/{code}/usual")
        sarah.post("/order/status", data={"status": "ordered"})
        ben.post("/me/drink", data={"base_id": "long_black"})
        page = T(sarah.get("/"))
        assert "<strong>Ben</strong> — flat white" in page and 'class="tag">one-off' not in page
        sarah.post("/order/status", data={"status": "ready"})
    with Session(get_engine()) as s:
        bases = sorted(d.base_id for d in s.exec(select(RoundDrink)).all())
    assert bases == ["flat_white", "flat_white"]


def test_a_placed_order_cannot_be_shared_again():
    from app.db import get_engine
    from app.models import Order

    with _client() as sarah, _client() as tom:
        _onboard(sarah, "Sarah", base_id="flat_white")
        code = _share(sarah)
        tom.post(f"/j/{code}/guest", data={"display_name": "Tom", "base_id": "latte"})
        sarah.post("/order/status", data={"status": "ordered"})
        sarah.post("/order/share", data={"when": "soon", "max_drinks": "3", "name": "x"})
    with Session(get_engine()) as s:
        order = s.exec(select(Order)).one()
    assert order.max_drinks == 8 and order.name is None and order.code == code


def test_a_stale_remove_leaves_a_ready_orders_guest_alone():
    with _client() as sarah, _client() as tom:
        _onboard(sarah, "Sarah", base_id="flat_white")
        code = _share(sarah)
        tom.post(f"/j/{code}/guest", data={"display_name": "Tom", "base_id": "latte"})
        tom_id = _user_id_by_name("Tom")
        sarah.post("/order/status", data={"status": "ready"})
        sarah.post(f"/order/remove/{tom_id}")
        r = T(tom.get(f"/j/{code}"))
    assert "Your drink is ready." in r


def test_a_one_off_person_belongs_to_one_order_only():
    with _client() as sarah:
        _onboard(sarah, "Sarah", base_id="flat_white")
        sarah.post("/people", data={"display_name": "Visitor", "one_off": "1", "base_id": "latte"})
        vid = _user_id_by_name("Visitor")
        r = sarah.post("/orders", data={"closes_at": "2099-01-01T09:00", "name": "Later"})
        # Too far ahead is refused; plan one inside the window instead.
        from datetime import datetime, timedelta

        when = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
        r = sarah.post("/orders", data={"closes_at": when, "name": "Later"})
        oid = int(r.headers["HX-Redirect"].rsplit("/", 1)[1])
        sarah.post(f"/orders/{oid}/add/{vid}")
        assert "<strong>Visitor</strong>" not in T(sarah.get(f"/orders/{oid}"))
        sarah.post("/order/clear")


def test_joiners_are_told_when_the_buyer_deletes_themselves():
    with _client() as sarah, _client() as tom:
        _onboard(sarah, "Sarah", base_id="flat_white")
        code = _share(sarah)
        tom.post(f"/j/{code}/guest", data={"display_name": "Tom", "base_id": "latte"})
        sarah.post("/me/delete")
        r = tom.get(f"/j/{code}")
    assert r.status_code == 200 and "has cancelled the order. Your drink has not been ordered." in T(r)


def test_no_one_is_made_for_a_finished_advance_order():
    from app.db import get_engine
    from app.models import User

    with _client() as sarah:
        _onboard(sarah, "Sarah", base_id="flat_white")
        r = sarah.post("/people", data={"display_name": "Ghost", "one_off": "1", "base_id": "latte", "order_id": "999"})
    assert r.headers.get("HX-Redirect") == "/"
    with Session(get_engine()) as s:
        assert s.exec(select(User).where(User.display_name == "Ghost")).first() is None


def test_ordering_early_keeps_an_advance_order_alive_until_after_its_closing_time():
    from datetime import datetime, timedelta, timezone

    from app.models import Order
    from app.orders import _expired

    now = datetime.now(timezone.utc)
    order = Order(owner_id="x", kind="advance", status="ordered", status_at=now,
                  joins_close_at=now + timedelta(hours=18))
    assert not _expired(order, now, now + timedelta(hours=20))
    assert _expired(order, now, now + timedelta(hours=31))


def test_a_joiner_in_a_full_order_is_not_told_it_is_closed():
    with _client() as sarah, _client() as tom:
        _onboard(sarah, "Sarah", base_id="flat_white")
        code = _share(sarah, max_drinks="2")
        r = T(tom.post(f"/j/{code}/guest", data={"display_name": "Tom", "base_id": "latte"}))
    assert "closed to new drinks" not in r.lower() and "Taking orders until" in r
    assert "Sarah's order has reached its limit of drinks. Your drink is still in the order." in r


# ---- Wording approved after the review ---------------------------------------


def test_the_home_page_and_the_panel_say_how_to_join_by_typing_the_code():
    with _client() as sarah:
        _onboard(sarah, "Sarah", base_id="flat_white")
        assert 'href="/join"' in T(sarah.get("/")) and "Join an order with a code" in T(sarah.get("/"))
        _share(sarah)
        page = T(sarah.get("/"))
    assert "<h2>Shared order</h2>" in page
    assert "Colleagues can also enter the code at testserver/join." in page


def test_a_joiner_is_told_their_drink_was_removed_and_a_colleagues_line_is_named():
    with _client() as sarah, _client() as tom:
        _onboard(sarah, "Sarah", base_id="flat_white")
        code = _share(sarah)
        tom.post(f"/j/{code}/guest", data={"display_name": "Tom", "base_id": "latte"})
        tom.post(f"/j/{code}/guest", data={"display_name": "Ben", "base_id": "piccolo", "mode": "colleague"})
        r = T(tom.post(f"/j/{code}/line/{_user_id_by_name('Tom')}/remove"))
        assert "Ben's drink is in Sarah's order." in r
        assert "Collected from Sarah" in r  # no location given
        r = T(tom.post(f"/j/{code}/line/{_user_id_by_name('Ben')}/remove"))
    # Ben was a colleague's drink, so no "your drink" note.
    assert "Your drink has been removed" not in r


def test_removing_your_own_last_drink_says_so():
    with _client() as sarah, _client() as tom:
        _onboard(sarah, "Sarah", base_id="flat_white")
        code = _share(sarah)
        tom.post(f"/j/{code}/guest", data={"display_name": "Tom", "base_id": "latte"})
        r = T(tom.post(f"/j/{code}/line/{_user_id_by_name('Tom')}/remove"))
    assert "Your drink has been removed from Sarah's order." in r


def test_the_clear_warning_counts_people_and_guests_by_code_are_not_tagged():
    with _client() as sarah, _client() as tom:
        _onboard(sarah, "Sarah", base_id="flat_white")
        code = _share(sarah)
        tom.post(f"/j/{code}/guest", data={"display_name": "Tom", "base_id": "latte"})
        tom.post(f"/j/{code}/guest", data={"display_name": "Ben", "base_id": "piccolo", "mode": "colleague"})
        page = T(sarah.get("/"))
    assert "One colleague has joined this order." in page
    assert 'class="tag">one-off' not in page
    assert "Clear the order" in page and ">Clear order<" not in page


def test_a_join_cookie_for_an_earlier_order_with_the_same_id_is_ignored():
    from datetime import timedelta

    from app.db import get_engine
    from app.joining import JoinCookie, order_key, write_join_cookie
    from app.models import Order

    with _client() as sarah, _client() as tom:
        _onboard(sarah, "Sarah", base_id="flat_white")
        code = _share(sarah)
        with Session(get_engine()) as s:
            order = s.exec(select(Order)).one()
            stale = Order(id=order.id, owner_id="x", created_at=order.created_at - timedelta(days=3))
        # A cookie from a pruned order that had the same id.
        from fastapi import Response
        from starlette.requests import Request

        resp = Response()
        write_join_cookie(Request({"type": "http", "scheme": "http", "headers": [], "path": "/", "server": ("testserver", 80), "query_string": b""}), resp,
                          JoinCookie(orders={order_key(stale): "g_old"}))
        cookie = resp.headers["set-cookie"].split(";")[0].split("=", 1)[1]
        tom.cookies.set("join", cookie)
        r = T(tom.get(f"/j/{code}"))
    assert "has removed your drink" not in r and "Your name:" in r


# ---- Fixes from the second review ----------------------------------------------


def _join_token(page: str) -> str:
    return re.search(r'name="joiner" value="([^"]+)"', page).group(1)


def test_a_guest_submitting_twice_adds_one_drink():
    with _client() as sarah, _client() as tom:
        _onboard(sarah, "Sarah", base_id="flat_white")
        code = _share(sarah)
        form = {"display_name": "Tom", "base_id": "latte"}
        tom.post(f"/j/{code}/guest", data=form)
        r = tom.post(f"/j/{code}/guest", data=form)
    assert len(_items()) == 1
    assert "Your drink is in Sarah's order." in T(r) and "Remove my drink" in T(r)


def test_a_guest_double_tapping_before_the_cookie_arrives_adds_one_drink():
    with _client() as sarah, _client() as tom:
        _onboard(sarah, "Sarah", base_id="flat_white")
        code = _share(sarah)
        form = {"display_name": "Tom", "base_id": "latte", "joiner": _join_token(T(tom.get(f"/j/{code}")))}
        tom.post(f"/j/{code}/guest", data=form)
        tom.cookies.clear()  # the second tap left before the first came back
        tom.post(f"/j/{code}/guest", data=form)
        r = tom.get(f"/j/{code}")
    assert len(_items()) == 1
    assert "Remove my drink" in T(r)


def test_a_forged_joiner_token_is_ignored():
    with _client() as sarah, _client() as tom:
        _onboard(sarah, "Sarah", base_id="flat_white")
        code = _share(sarah)
        tom.post(f"/j/{code}/guest", data={"display_name": "Tom", "base_id": "latte", "joiner": "g_forged"})
    assert _items()[0].joined_by != "g_forged"


def test_someone_added_from_the_roster_is_told_when_the_buyer_cancels():
    with _client() as sarah, _client() as priya:
        _onboard(priya, "Priya", base_id="latte")
        _onboard(sarah, "Sarah", base_id="flat_white")
        sarah.post(f"/order/add/{_user_id_by_name('Priya')}")
        code = _share(sarah)
        assert "Sarah has already added your usual drink" in T(priya.get(f"/j/{code}"))
        sarah.post("/order/clear")
        r = priya.get(f"/j/{code}")
    assert "Sarah has cancelled the order. Your drink has not been ordered." in T(r)


def test_a_joiner_on_a_replaced_code_is_told_when_the_buyer_cancels():
    with _client() as sarah, _client() as tom:
        _onboard(sarah, "Sarah", base_id="flat_white")
        code = _share(sarah)
        tom.post(f"/j/{code}/guest", data={"display_name": "Tom", "base_id": "latte"})
        sarah.post("/order/code/replace")
        sarah.post("/order/clear")
        r = tom.get(f"/j/{code}")
    assert r.status_code == 200
    assert "Sarah has cancelled the order." in T(r)


def test_a_joiner_who_retypes_the_code_is_never_blocked():
    from app.main import create_app

    app = create_app()  # one app, so that the three share its attempt counter
    with TestClient(app) as sarah, TestClient(app) as tom, TestClient(app) as typo:
        _onboard(sarah, "Sarah", base_id="flat_white")
        code = _share(sarah)
        tom.post(f"/j/{code}/guest", data={"display_name": "Tom", "base_id": "latte"})
        for _ in range(12):
            typo.get("/j/BBBBBB")
        assert typo.get(f"/join?code={code}", follow_redirects=False).status_code == 429
        assert tom.get(f"/j/{code}").status_code == 200
        r = tom.get(f"/join?code={code}", follow_redirects=False)
    assert r.status_code == 303


# ---- Stopping sharing --------------------------------------------------------


def test_stopping_sharing_before_anyone_joins_takes_the_order_back_to_private():
    with _client() as sarah, _client() as c:
        _onboard(sarah, "Sarah", base_id="latte")
        code = _share(sarah, name="Morning run", location="Tearoom", max_drinks="5")
        assert "Stop sharing" in sarah.get("/").text
        r = sarah.post("/order/share/stop", headers=HX)
        assert r.status_code == 200
        assert "Share this order" in r.text and "Stop sharing" not in r.text
        assert 'id="share-panel"' in r.text and 'id="advance-orders"' in r.text
        # The code no longer finds the order.
        assert c.get(f"/join?code={code}").status_code == 404
        # Sharing again starts afresh, with a new code and default settings.
        again = _share(sarah)
    assert again != code
    from app.db import get_engine
    from app.models import Order
    with Session(get_engine()) as s:
        order = s.exec(select(Order)).one()
    assert order.name is None and order.location is None and order.max_drinks == 8


def test_once_someone_joins_by_code_the_buyer_closes_joins_instead():
    with _client() as sarah, _client() as tom:
        _onboard(sarah, "Sarah", base_id="latte")
        code = _share(sarah)
        tom.post(f"/j/{code}/guest", data={"display_name": "Tom", "base_id": "latte"})
        home = sarah.get("/")
        assert "Stop sharing" not in home.text and "Close to new drinks" in home.text
        # A page shown before Tom joined still offers it; the server refuses.
        r = sarah.post("/order/share/stop", headers=HX)
        assert "A colleague has joined the order, so the order is still shared. You can close the order to new drinks instead." in T(r)
        assert tom.get(f"/j/{code}").status_code == 200
    assert len(_items()) == 1


def test_stop_sharing_is_offered_after_closing_while_nobody_has_joined():
    with _client() as sarah:
        _onboard(sarah, "Sarah", base_id="latte")
        _share(sarah)
        closed = sarah.post("/order/joins/close")
        assert "Reopen for 15 minutes" in closed.text and "Stop sharing" in closed.text



# ---- Public join links ------------------------------------------------------


def test_a_finished_order_answers_only_devices_that_joined_it():
    with _client() as sarah, _client() as tom, _client() as stranger:
        _onboard(sarah, "Sarah", base_id="flat_white")
        code = _share(sarah)
        tom.post(f"/j/{code}/guest", data={"display_name": "Tom", "base_id": "latte"})
        sarah.post("/order/clear")
        joined = tom.get(f"/j/{code}")
        other = stranger.get(f"/j/{code}")
    assert joined.status_code == 200 and "Sarah" in T(joined)
    assert other.status_code == 404 and "Sarah" not in T(other)


def test_a_join_link_never_gives_an_app_identity():
    from app.config import get_settings

    name = get_settings().cookie_name
    with _client() as sarah, _client() as tom:
        _onboard(sarah, "Sarah", base_id="flat_white")
        code = _share(sarah)
        page = tom.get(f"/j/{code}")
        joined = tom.post(f"/j/{code}/guest", data={"display_name": "Tom", "base_id": "latte"})
        after = tom.get(f"/j/{code}")
        for r in (page, joined, after):
            assert name not in r.headers.get("set-cookie", "")
        assert name not in tom.cookies


def test_the_share_panel_offers_the_join_link_to_copy():
    with _client() as sarah:
        _onboard(sarah, "Sarah", base_id="flat_white")
        code = _share(sarah)
        home = sarah.get("/")
    assert "Copy link" in home.text and "Share link" in home.text
    assert f"http://testserver/j/{code}" in home.text


# ---- A guest keeping their drink as their usual ----------------------------


def test_a_guest_can_save_their_drink_as_their_usual():
    from app.db import get_engine
    from app.models import SavedDrink

    with _client() as sarah, _client() as tom:
        _onboard(sarah, "Sarah", base_id="flat_white")
        code = _share(sarah)
        page = tom.post(f"/j/{code}/guest", data={"display_name": "Tom", "base_id": "long_black"})
        assert f'<a href="/join/save/{code}">Save this as my usual drink</a>' in page.text
        r = tom.get(f"/join/save/{code}", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/"
        home = T(tom.get("/"))
        tom_id = _user_id_by_name("Tom")
        with Session(get_engine()) as s:
            assert s.get(SavedDrink, tom_id).base_id == "long_black"
        # Signed in as Tom now, so the join page offers his usual drink and
        # no longer the link.
        assert "Tom" in home
        assert "Save this as my usual drink" not in tom.get(f"/j/{code}").text
        again = tom.get(f"/join/save/{code}", follow_redirects=False)
        assert again.headers["location"] == "/"
    # The guest line in Sarah's order stays a guest.
    assert len([i for i in _items() if i.joined_by is not None]) == 1


def _taken_name_setup(sarah, tom, old_tom):
    _onboard(old_tom, "Tom", base_id="flat_white")
    _onboard(sarah, "Sarah", base_id="flat_white")
    code = _share(sarah)
    tom.post(f"/j/{code}/guest", data={"display_name": "tom", "base_id": "long_black"})
    return code


def test_a_taken_name_shows_the_drink_that_would_be_replaced():
    with _client() as sarah, _client() as tom, _client() as old_tom:
        code = _taken_name_setup(sarah, tom, old_tom)
        r = tom.get(f"/join/save/{code}")
    page = T(r)
    assert r.status_code == 200 and "<h1>Tom is already on the list.</h1>" in page
    assert '<s class="person-card-old">flat white</s>' in page and "long black" in page
    assert "I am Tom, so replace the drink</button>" in page
    assert 'value="tom"' in page and "Back to the order" in page


def test_i_am_tom_replaces_the_existing_persons_drink():
    from app.db import get_engine
    from app.models import SavedDrink

    with _client() as sarah, _client() as tom, _client() as old_tom:
        code = _taken_name_setup(sarah, tom, old_tom)
        r = tom.post(f"/join/save/{code}/claim", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/"
        assert "Hi, Tom" in T(tom.get("/"))
    tom_id = _user_id_by_name("Tom")
    with Session(get_engine()) as s:
        assert s.get(SavedDrink, tom_id).base_id == "long_black"


def test_a_different_tom_saves_under_another_name():
    with _client() as sarah, _client() as tom, _client() as old_tom:
        code = _taken_name_setup(sarah, tom, old_tom)
        taken = tom.post(f"/join/save/{code}/new", data={"display_name": "TOM"})
        assert taken.status_code == 422 and "That name is already on the list." in T(taken)
        empty = tom.post(f"/join/save/{code}/new", data={"display_name": " "})
        assert empty.status_code == 422 and "Please enter your name." in T(empty)
        r = tom.post(f"/join/save/{code}/new", data={"display_name": "Tom B"}, follow_redirects=False)
        assert r.headers["location"] == "/"
        assert "Hi, Tom B" in T(tom.get("/"))
    assert _user_id_by_name("Tom") != _user_id_by_name("Tom B")


def test_a_matching_drink_only_asks_who_you_are():
    with _client() as sarah, _client() as tom, _client() as old_tom:
        _onboard(old_tom, "Tom", base_id="long_black")
        _onboard(sarah, "Sarah", base_id="flat_white")
        code = _share(sarah)
        tom.post(f"/j/{code}/guest", data={"display_name": "Tom", "base_id": "long_black"})
        page = T(tom.get(f"/join/save/{code}"))
    assert "<s " not in page and "→" not in page and "I am Tom</button>" in page


def test_saving_a_usual_needs_a_guest_line_on_this_device():
    with _client() as sarah, _client() as stranger:
        _onboard(sarah, "Sarah", base_id="flat_white")
        code = _share(sarah)
        r = stranger.get(f"/join/save/{code}", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/"
        assert stranger.get("/join/save/BKTWQV", follow_redirects=False).status_code == 303
    from app.db import get_engine
    from app.models import User

    with Session(get_engine()) as s:
        assert len(s.exec(select(User).where(User.display_name != None)).all()) == 1  # noqa: E711


# ---- The buyer: replacing the code, and notices of new drinks ------------------


def test_replacing_the_code_is_confirmed_in_a_dialog():
    with _client() as sarah:
        _onboard(sarah, "Sarah", base_id="flat_white")
        _share(sarah)
        panel = T(sarah.get("/"))
    assert '@click="confirming = true"' in panel
    assert "Colleagues who have not joined yet will no longer be able to use the current code or link." in panel
    dialog = panel.split('class="share-replace-dialog"', 1)[1].split("</dialog>", 1)[0]
    assert 'hx-post="/order/code/replace">Replace the code</button>' in dialog


def test_the_share_panel_lists_drinks_added_by_code_for_notices():
    import json

    with _client() as sarah, _client() as tom:
        _onboard(sarah, "Sarah", base_id="flat_white")
        code = _share(sarah)
        empty = sarah.get("/").text
        assert "data-joins='[]'" in empty
        assert "Notify me when a colleague adds a drink" in empty
        tom.post(f"/j/{code}/guest", data={"display_name": "Tom", "base_id": "long_black"})
        home = sarah.get("/").text
    joins = json.loads(html.unescape(re.search(r"data-joins='([^']*)'", home).group(1)))
    assert [(j["name"], "long black" in j["drink"].lower()) for j in joins] == [("Tom", True)]


def test_the_service_worker_is_served_from_the_root():
    with _client() as c:
        r = c.get("/sw.js")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/javascript")
    assert "showNotification" not in r.text and "notificationclick" in r.text


def test_adding_someone_whose_name_is_taken_offers_that_person():
    with _client() as sarah, _client() as tom:
        _onboard(tom, "Tom", base_id="long_black")
        _onboard(sarah, "Sarah", base_id="flat_white")
        page = sarah.get("/").text
    import json

    existing = json.loads(html.unescape(re.search(r"existing: (\{.*\}),\n", page).group(1)))
    assert existing["tom"] == {"id": _user_id_by_name("Tom"), "name": "Tom", "drink": "long black"}
    assert "' to the order'" in page and "/order/add/' + taken.id" in page
