"""Advance orders: planning one (O1b), the list and reminder on the home
page (O0, O0b), the order's own page (O2b) and joining it (J3c)."""

import html
import re
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlmodel import Session, select


def T(response) -> str:
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


def _tomorrow_4pm() -> str:
    from app import sharing

    local = datetime.now(timezone.utc) + timedelta(days=1)
    return sharing.local_input_value(local)[:11] + "16:00"


def _plan(buyer, **form) -> int:
    data = {"when": "chosen", "closes_at": _tomorrow_4pm(), "name": "Tuesday education meeting",
            "max_drinks": "12", **form}
    r = buyer.post("/orders", data=data)
    assert r.status_code == 204, r.text
    return int(r.headers["HX-Redirect"].rsplit("/", 1)[1])


def _order(order_id: int):
    from app.db import get_engine
    from app.models import Order

    with Session(get_engine()) as s:
        return s.get(Order, order_id)


def _close_joins(order_id: int):
    from app.db import get_engine
    from app.models import Order

    with Session(get_engine()) as s:
        order = s.get(Order, order_id)
        order.joins_close_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        s.add(order)
        s.commit()


def test_the_home_page_offers_to_plan_an_advance_order():
    with _client() as sarah:
        _onboard(sarah, "Sarah", base_id="flat_white")
        page = T(sarah.get("/"))
        assert "Plan an advance order" in page
        form = T(sarah.get("/orders/new"))
    assert "Order name:" in form and "Order name (optional):" not in form
    assert "In 30 minutes" not in form  # an advance order always has a chosen time
    assert 'hx-post="/orders"' in form


def test_planning_needs_a_name_and_a_time_in_the_future():
    with _client() as sarah:
        _onboard(sarah, "Sarah", base_id="flat_white")
        r = sarah.post("/orders", data={"closes_at": _tomorrow_4pm(), "name": " "})
        assert r.status_code == 422 and "Please enter a name for the order." in T(r)
        r = sarah.post("/orders", data={"closes_at": "2020-01-01T09:00", "name": "Audit"})
    assert r.status_code == 422 and "Please choose a closing time in the future." in T(r)


def test_an_advance_order_has_its_own_page_and_leaves_the_everyday_order_alone():
    with _client() as sarah, _client() as ben:
        _onboard(sarah, "Sarah", base_id="flat_white")
        _onboard(ben, "Ben", base_id="latte")
        oid = _plan(sarah, location="Seminar room 2")
        page = T(sarah.get(f"/orders/{oid}"))
        assert "<h1>Tuesday education meeting</h1>" in page
        assert "Back to the home page" in page
        assert 'class="share-code-text"' in page
        assert "Change closing time" in page and "Allow 15 more minutes" not in page
        assert "Seminar room 2" in page
        assert f'hx-post="/orders/{oid}/add/' in page
        # Adding Ben to the advance order leaves the everyday order empty.
        sarah.post(f"/orders/{oid}/add/{_user_id_by_name('Ben')}")
        assert "<strong>Ben</strong>" in T(sarah.get(f"/orders/{oid}"))
        home = T(sarah.get("/"))
    assert "<strong>Ben</strong>" not in home.split('id="advance-orders"')[0]
    assert "Advance orders" in home and "Tuesday education meeting" in home
    assert "2 of 12 drinks" in home  # Sarah and Ben


def test_someone_elses_advance_order_is_not_reachable():
    with _client() as sarah, _client() as ben:
        _onboard(sarah, "Sarah", base_id="flat_white")
        _onboard(ben, "Ben", base_id="latte")
        oid = _plan(sarah)
        r = ben.get(f"/orders/{oid}", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/"
        ben.post(f"/orders/{oid}/add/{_user_id_by_name('Ben')}")
        ben.post(f"/orders/{oid}/clear")
    assert _order(oid).status == "open"


def test_joining_an_advance_order_shows_its_name_date_and_location():
    with _client() as sarah, _client() as priya:
        _onboard(sarah, "Sarah", base_id="flat_white")
        _onboard(priya, "Priya", base_id="flat_white", milk="oat")
        oid = _plan(sarah, location="Seminar room 2")
        code = _order(oid).code
        landing = T(priya.get(f"/j/{code}"))
        assert "Tuesday education meeting" in landing
        assert re.search(r"Sarah is taking orders until 4:00 pm on \w+day \d+ \w+\.", landing)
        assert "Seminar room 2" in landing and 'name="location"' not in landing
        priya.post(f"/j/{code}/usual")
        # The buyer opening their own code lands on the order's page.
        r = sarah.get(f"/j/{code}", follow_redirects=False)
    assert r.headers["location"] == f"/orders/{oid}"
    assert "2 of 12 drinks" in T(sarah.get("/"))


def test_a_closed_advance_order_reminds_the_buyer_until_it_is_placed():
    with _client() as sarah, _client() as tom:
        _onboard(sarah, "Sarah", base_id="flat_white")
        oid = _plan(sarah)
        code = _order(oid).code
        tom.post(f"/j/{code}/guest", data={"display_name": "Tom", "base_id": "latte"})
        assert "has not been placed yet" not in T(sarah.get("/"))
        _close_joins(oid)
        home = T(sarah.get("/"))
        assert re.search(
            r"Joins for Tuesday education meeting closed at [^.]+\. "
            r"The order has two drinks and has not been placed yet\.", home
        )
        assert f'href="/orders/{oid}"' in home and "Open the order" in home
        sarah.post(f"/orders/{oid}/status", data={"status": "ordered"})
        placed = T(sarah.get("/"))
    assert "has not been placed yet" not in placed
    assert "Ordered at" in placed  # the list shows where it has got to


def test_the_closing_time_can_be_moved_or_closed_at_once():
    with _client() as sarah:
        _onboard(sarah, "Sarah", base_id="flat_white")
        oid = _plan(sarah)
        later = _tomorrow_4pm()[:11] + "17:30"
        r = T(sarah.post(f"/orders/{oid}/closing", data={"closes_at": later}))
        assert "5:30 pm" in r
        too_far = (datetime.now() + timedelta(days=9)).strftime("%Y-%m-%dT%H:%M")
        r = sarah.post(f"/orders/{oid}/closing", data={"closes_at": too_far})
        assert r.status_code == 422 and "The closing time can be at most seven days ahead." in T(r)
        r = T(sarah.post(f"/orders/{oid}/closing", data={"closes_at": "2020-01-01T09:00"}))
    assert 'aria-current="step">Locked</li>' in r


def test_ready_on_an_advance_order_completes_it_and_its_page_goes_home():
    with _client() as sarah, _client() as tom:
        _onboard(sarah, "Sarah", base_id="flat_white")
        oid = _plan(sarah)
        tom.post(f"/j/{_order(oid).code}/guest", data={"display_name": "Tom", "base_id": "latte"})
        page = T(sarah.get(f"/orders/{oid}"))
        assert "Lock the order" in page and f'hx-post="/orders/{oid}/status"' not in page
        locked = T(sarah.post(f"/orders/{oid}/joins/close"))
        assert f'hx-post="/orders/{oid}/status"' in locked and "Tell colleagues it is ready" in locked
        done = T(sarah.post(f"/orders/{oid}/status", data={"status": "ready"}))
        assert "This order is complete." in done and "Back to the home page" in done
        assert _order(oid).status == "ready"
        r = sarah.get(f"/orders/{oid}", follow_redirects=False)
    assert r.status_code == 303


def test_clearing_an_advance_order_goes_home():
    with _client() as sarah:
        _onboard(sarah, "Sarah", base_id="flat_white")
        oid = _plan(sarah)
        r = sarah.post(f"/orders/{oid}/clear", headers={"HX-Request": "true"})
    assert r.headers.get("HX-Redirect") == "/"
    assert _order(oid).status == "cancelled"


def test_a_person_made_on_an_advance_page_joins_that_order():
    with _client() as sarah:
        _onboard(sarah, "Sarah", base_id="flat_white")
        oid = _plan(sarah)
        page = T(sarah.get(f"/orders/{oid}"))
        assert f'name="order_id" value="{oid}"' in page
        sarah.post("/people", data={"display_name": "Visitor", "one_off": "1", "base_id": "latte", "order_id": str(oid)})
        assert "<strong>Visitor</strong>" in T(sarah.get(f"/orders/{oid}"))
        home = T(sarah.get("/"))
    assert "<strong>Visitor</strong>" not in home


def test_editing_a_person_from_an_advance_page_returns_to_it():
    with _client() as sarah:
        _onboard(sarah, "Sarah", base_id="flat_white")
        oid = _plan(sarah)
        sarah.post("/people", data={"display_name": "Visitor", "one_off": "1", "base_id": "latte", "order_id": str(oid)})
        vid = _user_id_by_name("Visitor")
        page = T(sarah.get(f"/orders/{oid}"))
        assert f'/people/{vid}/edit?order={oid}' in page
        form = T(sarah.get(f"/people/{vid}/edit?order={oid}"))
        assert f'hx-post="/people/{vid}/drink?order={oid}"' in form
        assert f'hx-get="/orders/{oid}"' in form
        saved = T(sarah.post(f"/people/{vid}/drink?order={oid}", data={"base_id": "long_black"}))
    assert "<strong>Visitor</strong> — long black" in saved
    assert f'hx-post="/orders/{oid}/remove/{vid}?from=list"' in saved


def test_the_everyday_order_and_an_advance_order_coexist_for_one_buyer():
    with _client() as sarah, _client() as ben:
        _onboard(sarah, "Sarah", base_id="flat_white")
        _onboard(ben, "Ben", base_id="latte")
        a = _plan(sarah)
        b = _plan(sarah, name="Friday audit lunch")
        ben_id = _user_id_by_name("Ben")
        sarah.post(f"/order/add/{ben_id}")
        sarah.post(f"/orders/{a}/add/{ben_id}")
        sarah.post(f"/orders/{b}/add/{ben_id}")
        home = T(sarah.get("/"))
    assert home.count('class="advance-link"') == 2
    assert "Friday audit lunch" in home


def test_the_home_page_statement_count_is_unchanged_by_advance_orders():
    with _client() as sarah:
        _onboard(sarah, "Sarah", base_id="flat_white")
        before = sarah.get("/").headers["server-timing"]
        _plan(sarah)
        _plan(sarah, name="Friday audit lunch")
        after = sarah.get("/").headers["server-timing"]
    def count(header):
        return int(re.search(r"(\d+) queries", header).group(1))

    # The advance orders come back with the everyday order's own statement.
    assert count(after) == count(before)


def test_an_advance_order_no_one_joined_can_still_be_placed_or_cleared():
    with _client() as sarah:
        _onboard(sarah, "Sarah", base_id="flat_white")
        oid = _plan(sarah)
        page = T(sarah.get(f"/orders/{oid}"))
        locked = T(sarah.post(f"/orders/{oid}/joins/close"))
    assert f'hx-post="/orders/{oid}/joins/close"' in page
    assert f'hx-post="/orders/{oid}/status"' in locked  # Ready, though no one joined
    assert f'hx-post="/orders/{oid}/clear"' in page
    assert "Drinks allowed:" in page  # the cap, before the order is full


def test_sharing_and_planning_open_in_a_dialog_from_one_row():
    with _client() as sarah:
        _onboard(sarah, "Sarah", base_id="flat_white")
        home = T(sarah.get("/"))
        panel = home.split('id="share-panel"', 1)[1].split("</div>\n</div>", 1)[0]
        assert "Share this order" in panel and "Plan an advance order" in panel
        assert 'id="share-dialog"' in home
        for url, title in (("/order/share", "Share this order"), ("/orders/new", "Plan an advance order")):
            form = T(sarah.get(url))
            assert form.lstrip().startswith("<dialog") and f"<h2>{title}</h2>" in form
        # A refused form comes back into the dialog with its error.
        r = sarah.post("/orders", data={"closes_at": _tomorrow_4pm(), "name": " "})
        assert r.status_code == 422 and "<dialog" in T(r) and "Please enter a name for the order." in T(r)
        # Sharing empties the dialog and swaps the panel and the planning
        # button in out of band.
        shared = T(sarah.post("/order/share", data={"when": "soon"}))
        assert "new-person-dialog share-dialog" not in shared  # the sharing form is gone
        assert 'id="share-panel"' in shared and 'class="share-code-text"' in shared
        assert 'id="advance-orders" hx-swap-oob="true"' in shared and "Plan an advance order" in shared
        _plan(sarah)
        home = T(sarah.get("/"))
    assert home.count("Plan an advance order") == 1  # with the list, once


def test_a_finished_advance_order_sends_its_page_home_from_every_control():
    with _client() as sarah:
        _onboard(sarah, "Sarah", base_id="flat_white")
        with _client() as priya:
            _onboard(priya, "Priya", base_id="latte")
        oid = _plan(sarah)
        page = T(sarah.get(f"/orders/{oid}"))
        version = re.search(rf'/orders/{oid}/live\?v=([0-9a-f]+)', page).group(1)
        # Finished elsewhere, in another tab.
        sarah.post(f"/orders/{oid}/clear", headers={"HX-Request": "true"})
        hx = {"HX-Request": "true"}
        priya_id = _user_id_by_name("Priya")
        for r in (
            sarah.get(f"/orders/{oid}/live?v={version}", headers=hx),
            sarah.post(f"/orders/{oid}/add/{priya_id}", headers=hx),
            sarah.post(f"/orders/{oid}/remove/{priya_id}", headers=hx),
            sarah.post(f"/orders/{oid}/joins/close", headers=hx),
            sarah.post(f"/orders/{oid}/status", data={"status": "ordered"}, headers=hx),
            sarah.get(f"/orders/{oid}/share/panel", headers=hx),
            sarah.get(f"/people/{priya_id}/edit?order={oid}", headers=hx),
        ):
            assert r.headers.get("HX-Redirect") == "/", r.request.url


def test_an_advance_order_is_never_offered_stop_sharing():
    with _client() as sarah:
        _onboard(sarah, "Sarah", base_id="latte")
        oid = _plan(sarah)
        page = sarah.get(f"/orders/{oid}")
    assert page.status_code == 200 and "Stop sharing" not in page.text
