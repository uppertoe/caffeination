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
        _onboard(bob, "Bob", base_id="espresso")
        alice_id = _user_id_by_name("Alice")

        r = bob.post(f"/order/add/{alice_id}")
        assert r.status_code == 200
        # Bob's order now lists both their drink and Alice's drink.
        assert "espresso" in r.text
        assert "large oat flat white" in r.text

        r = bob.post(f"/order/remove/{alice_id}")
        assert r.status_code == 200
        # Alice moves back to the roster (Add button) and out of the order.
        # Look for the Add affordance specifically rather than just her name.
        assert 'hx-post="/order/add/' in r.text


def test_saving_own_drink_triggers_order_section_refresh():
    """The drink form POST fires the order-refresh event so the order section
    refetches itself and 'Alice — oat latte' appears without a page reload.
    (An event rather than an OOB swap so an open inline person-edit form,
    which carries no listener, is never clobbered.)"""
    with _client() as alice:
        alice.get("/")
        alice.post("/me/name", data={"display_name": "Alice"})
        r = alice.post(
            "/me/drink",
            data={"base_id": "latte", "size": "regular", "milk": "oat"},
        )
        assert r.status_code == 200
        assert r.headers["HX-Trigger"] == "order-refresh"
        # The refresh the event triggers in the browser:
        r = alice.get("/order")
    assert "Alice" in r.text
    assert "oat latte" in r.text


def test_owner_cant_add_self_to_order_via_add_endpoint():
    with _client() as alice:
        _onboard(alice, "Alice", base_id="latte", size="regular", milk="oat")
        alice_id = _user_id_by_name("Alice")
        r = alice.post(f"/order/add/{alice_id}")
        assert r.status_code == 200
        # Alice still appears once (the owner row); self-add is a no-op.
        assert r.text.count("Alice") == 1


def test_owner_can_remove_self_then_add_back():
    with _client() as alice:
        _onboard(alice, "Alice", base_id="latte", size="regular", milk="oat")
        alice_id = _user_id_by_name("Alice")

        r = alice.post(f"/order/remove/{alice_id}")
        assert r.status_code == 200
        # Her drink drops out of the till summary and an add-back button shows.
        assert "1x regular oat latte" not in r.text
        assert "Your order is empty." in r.text
        assert f'hx-post="/order/add/{alice_id}"' in r.text

        r = alice.post(f"/order/add/{alice_id}")
        assert r.status_code == 200
        assert "1x regular oat latte" in r.text
        assert f'hx-post="/order/add/{alice_id}"' not in r.text


def test_self_removal_survives_rerender_and_only_affects_own_order():
    with _client() as alice, _client() as bob:
        _onboard(alice, "Alice", base_id="latte", size="regular", milk="oat")
        _onboard(bob, "Bob", base_id="espresso")
        alice_id = _user_id_by_name("Alice")

        alice.post(f"/order/remove/{alice_id}")
        r = alice.get("/")
        assert "1x regular oat latte" not in r.text  # sticks across page loads

        # Bob can still add Alice to *his* order.
        r = bob.post(f"/order/add/{alice_id}")
        assert "regular oat latte" in r.text


def test_clear_order_resets_self_removal():
    with _client() as alice, _client() as bob:
        _onboard(alice, "Alice", base_id="latte", size="regular", milk="oat")
        _onboard(bob, "Bob", base_id="espresso")
        alice_id = _user_id_by_name("Alice")
        bob_id = _user_id_by_name("Bob")

        alice.post(f"/order/remove/{alice_id}")
        alice.post(f"/order/add/{bob_id}")
        r = alice.post("/order/clear")
        # Cleared order is back to the default: Alice included, Bob gone
        # (his espresso is a roster line again, not a till line).
        assert "1x regular oat latte" in r.text
        assert "1x espresso" not in r.text
        assert f'hx-post="/order/remove/{bob_id}"' not in r.text


def _backdate_activity(user_id: str, days: int) -> None:
    from datetime import datetime, timedelta, timezone

    from sqlmodel import Session

    from app.db import get_engine
    from app.models import User

    with Session(get_engine()) as s:
        u = s.get(User, user_id)
        u.last_active_at = datetime.now(timezone.utc) - timedelta(days=days)
        s.add(u)
        s.commit()


def test_inactive_users_collapse_behind_expander():
    with _client() as alice, _client() as bob:
        _onboard(alice, "Alice", base_id="latte", size="regular", milk="oat")
        _onboard(bob, "Bob", base_id="espresso")
        alice_id = _user_id_by_name("Alice")
        _backdate_activity(alice_id, days=120)

        r = bob.get("/")
        # Alice is offered, but as a collapsed inactive entry. The count is
        # server-rendered inside the span Alpine keeps live after an Add.
        assert "roster-expand" in r.text
        assert 'x-text="inactive.length">1</span> inactive' in r.text
        assert f'hx-post="/order/add/{alice_id}"' in r.text
        assert "inactive-row" in r.text


def test_active_users_have_no_expander():
    with _client() as alice, _client() as bob:
        _onboard(alice, "Alice", base_id="latte", size="regular", milk="oat")
        _onboard(bob, "Bob", base_id="espresso")
        r = bob.get("/")
        # No expander row and no collapsed rows; the Alpine state still
        # declares an (empty) inactive list, so don't grep the bare word.
        assert "roster-expand" not in r.text
        assert "inactive-row" not in r.text


def test_being_added_to_an_order_reactivates_a_user():
    with _client() as alice, _client() as bob, _client() as carol:
        _onboard(alice, "Alice", base_id="latte", size="regular", milk="oat")
        _onboard(bob, "Bob", base_id="espresso")
        _onboard(carol, "Carol", base_id="flat_white", size="regular", milk="oat")
        alice_id = _user_id_by_name("Alice")
        _backdate_activity(alice_id, days=120)

        # Bob picks Alice for his run: that is a sign of life.
        bob.post(f"/order/add/{alice_id}")

        # Carol's roster now shows Alice as active again.
        r = carol.get("/")
        assert "roster-expand" not in r.text
        assert "inactive-row" not in r.text
        assert f'hx-post="/order/add/{alice_id}"' in r.text


def test_visiting_refreshes_own_activity():
    with _client() as alice:
        _onboard(alice, "Alice", base_id="latte", size="regular", milk="oat")
        alice_id = _user_id_by_name("Alice")
        _backdate_activity(alice_id, days=120)

        alice.get("/")  # any authenticated request counts

        from sqlmodel import Session

        from app.db import get_engine
        from app.models import User
        from app.users import _as_naive_utc

        from datetime import datetime, timedelta, timezone

        with Session(get_engine()) as s:
            u = s.get(User, alice_id)
            age = _as_naive_utc(datetime.now(timezone.utc)) - _as_naive_utc(
                u.last_active_at
            )
            assert age < timedelta(minutes=5)


def test_adding_unknown_target_is_noop():
    with _client() as alice:
        _onboard(alice, "Alice", base_id="latte", size="regular", milk="oat")
        r = alice.post("/order/add/nonexistent")
        assert r.status_code == 200
        # Still just Alice (the owner row).
        assert r.text.count("Alice") == 1


def test_adding_someone_elses_one_off_is_noop():
    """One-off guests live only in their creator's order — another owner
    holding a reference would be orphaned when the creator removes them."""
    with _client() as alice, _client() as bob:
        _onboard(alice, "Alice", base_id="latte", size="regular", milk="oat")
        _onboard(bob, "Bob", base_id="espresso")
        alice.post(
            "/people",
            data={"display_name": "Guest", "base_id": "latte", "one_off": "1"},
        )
        guest_id = _user_id_by_name("Guest")

        r = bob.post(f"/order/add/{guest_id}")
        assert r.status_code == 200
        assert f'hx-post="/order/remove/{guest_id}"' not in r.text
        assert "Guest" not in r.text


# ---------------------------------------------------------------------------
# Add from a roster row: the row is the swap target, the rest comes back OOB
# ---------------------------------------------------------------------------


def test_add_from_roster_row_returns_only_changed_articles_oob():
    with _client() as alice, _client() as bob:
        _onboard(alice, "Alice", base_id="flat_white", size="large", milk="oat")
        _onboard(bob, "Bob", base_id="espresso")
        alice_id = _user_id_by_name("Alice")

        page = bob.get("/").text
        # The roster row carries the id htmx will report as HX-Target, and
        # its Add button swaps just that row.
        assert f'id="roster-{alice_id}"' in page
        assert 'hx-target="closest li"' in page and 'hx-swap="outerHTML"' in page

        r = bob.post(f"/order/add/{alice_id}", headers={"HX-Target": f"roster-{alice_id}"})
        assert r.status_code == 200
        # The row comes back selected, in place, with a Remove button...
        assert f'id="roster-{alice_id}"' in r.text and "is-selected" in r.text
        assert f'hx-post="/order/remove/{alice_id}" hx-target="closest li"' in r.text
        # ...with both changed articles out of band; no roster, no section.
        assert r.text.count('hx-swap-oob="true"') == 2
        assert 'id="order-list"' in r.text and 'id="till-summary"' in r.text
        assert 'id="order-section"' not in r.text
        assert "roster-list" not in r.text
        assert "1x large oat flat white" in r.text

        # Removing from the row deselects it the same way.
        r = bob.post(f"/order/remove/{alice_id}", headers={"HX-Target": f"roster-{alice_id}"})
        assert f'id="roster-{alice_id}"' in r.text and "is-selected" not in r.text
        assert f'hx-post="/order/add/{alice_id}"' in r.text
        assert "large oat flat white" not in r.text.split('id="till-summary"')[1]

        # The selected person stays on the roster rather than disappearing.
        bob.post(f"/order/add/{alice_id}")
        page = bob.get("/").text
        assert f'id="roster-{alice_id}"' in page and "is-selected" in page


def test_add_without_row_target_returns_full_section():
    """The 'Add me' opt-in, tests and curl target the whole section."""
    with _client() as alice, _client() as bob:
        _onboard(alice, "Alice", base_id="latte")
        _onboard(bob, "Bob", base_id="espresso")
        alice_id = _user_id_by_name("Alice")
        r = bob.post(f"/order/add/{alice_id}")
        assert 'id="order-section"' in r.text
        assert 'id="roster-picker"' in r.text
        # The right-hand column is its own element, so it comes back out of
        # band with the section; nothing else does.
        assert r.text.count('hx-swap-oob') == 1
        assert 'id="order-side" class="order-side" hx-swap-oob="true"' in r.text


def test_remove_from_your_order_swaps_only_what_changed():
    with _client() as alice, _client() as bob:
        _onboard(alice, "Alice", base_id="flat_white", size="large", milk="oat")
        _onboard(bob, "Bob", base_id="espresso")
        alice_id = _user_id_by_name("Alice")
        bob.post(f"/order/add/{alice_id}")
        page = bob.get("/").text
        assert f'hx-post="/order/remove/{alice_id}?from=list"' in page and 'hx-swap="none"' in page

        r = bob.post(f"/order/remove/{alice_id}?from=list")
        # The order list, the till summary and Alice's roster row, all out of
        # band; never the section, so the roster and scroll stay put.
        assert r.text.count('hx-swap-oob="true"') == 3
        assert 'id="order-section"' not in r.text and "roster-list" not in r.text
        assert f'id="roster-{alice_id}" hx-swap-oob="true"' in r.text
        roster_row = r.text.split(f'id="roster-{alice_id}"')[1]
        assert "is-selected" not in roster_row.split("</li>")[0]
        assert "large oat flat white" not in r.text.split('id="till-summary"')[1].split("</article>")[0]


def test_owner_keyed_order_lines_move_onto_orders():
    """A database from before the Order table keyed lines by owner, with a
    self-targeting row as the opt-out marker. init_db turns each owner's lines
    into one open everyday order, the marker into owner_excluded_at, and
    leaves the app working on the result."""
    import sqlite3

    from app.config import get_settings
    from app.db import init_db
    from app.orders import is_self_excluded, load_order_view

    path = get_settings().database_url.removeprefix("sqlite:///")
    con = sqlite3.connect(path)
    con.executescript(
        'CREATE TABLE "user" (id VARCHAR PRIMARY KEY, display_name VARCHAR, created_at TIMESTAMP,'
        " created_by VARCHAR, one_off BOOLEAN, last_active_at TIMESTAMP);"
        "CREATE TABLE orderitem (owner_id VARCHAR, target_user_id VARCHAR, added_at TIMESTAMP,"
        " PRIMARY KEY (owner_id, target_user_id));"
        "CREATE INDEX ix_orderitem_target ON orderitem (target_user_id);"
        "INSERT INTO \"user\" VALUES ('me', 'Me', '2026-01-05 08:00:00', NULL, 0, '2026-01-05 08:00:00');"
        "INSERT INTO \"user\" VALUES ('ann', 'Ann', '2026-01-05 08:00:00', NULL, 0, '2026-01-05 08:00:00');"
        "INSERT INTO \"user\" VALUES ('bob', 'Bob', '2026-01-05 08:00:00', NULL, 0, '2026-01-05 08:00:00');"
        # Me has Ann in their order and has opted out; Bob has Ann too.
        "INSERT INTO orderitem VALUES ('me', 'ann', '2026-01-05 09:00:00');"
        "INSERT INTO orderitem VALUES ('me', 'me', '2026-01-05 09:05:00');"
        "INSERT INTO orderitem VALUES ('bob', 'ann', '2026-01-05 09:10:00');"
    )
    con.commit()
    con.close()

    init_db()
    init_db()  # a second run is a no-op

    con = sqlite3.connect(path)
    orders = con.execute(
        "SELECT owner_id, kind, status, owner_excluded_at, created_at FROM grouporder ORDER BY owner_id"
    ).fetchall()
    lines = con.execute(
        "SELECT g.owner_id, o.target_user_id, o.added_at FROM orderitem o"
        " JOIN grouporder g ON g.id = o.order_id ORDER BY g.owner_id"
    ).fetchall()
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    con.close()
    assert orders == [
        ("bob", "everyday", "open", None, "2026-01-05 09:10:00"),
        ("me", "everyday", "open", "2026-01-05 09:05:00", "2026-01-05 09:00:00"),
    ]
    assert lines == [("bob", "ann", "2026-01-05 09:10:00"), ("me", "ann", "2026-01-05 09:00:00")]
    assert "orderitem_by_owner" not in tables

    from sqlmodel import Session

    from app.db import get_engine
    from app.models import User

    with Session(get_engine()) as s:
        assert is_self_excluded(s, "me")
        me = s.get(User, "me")
        from app.orders import open_everyday_order

        order = open_everyday_order(s, "me")
        assert order is not None
        # Being months old, the order is closed as expired on the first render.
        assert load_order_view(s, me).rows == []
        s.refresh(order)
        assert order.status == "expired"


def test_two_first_adds_at_once_share_one_everyday_order(monkeypatch):
    """Two Add taps on an empty order, handled side by side: each looked for
    the everyday order before the other made it. Simulated by hiding the
    order from the second tap's lookup."""
    from sqlmodel import Session, select

    from app import orders
    from app.db import get_engine
    from app.models import Order, OrderItem

    with _client() as alice, _client() as bob, _client() as carol:
        _onboard(alice, "Alice", base_id="latte")
        _onboard(carol, "Carol", base_id="latte")
        _onboard(bob, "Bob", base_id="espresso")
        bob.post(f"/order/add/{_user_id_by_name('Alice')}")
        real = orders.live_order
        calls = []

        def stale_once(session, owner_id, order_id=None):
            calls.append(1)
            return None if len(calls) == 1 else real(session, owner_id, order_id)

        monkeypatch.setattr(orders, "live_order", stale_once)
        r = bob.post(f"/order/add/{_user_id_by_name('Carol')}")
    assert r.status_code == 200
    with Session(get_engine()) as s:
        assert len(s.exec(select(Order)).all()) == 1
        assert len(s.exec(select(OrderItem)).all()) == 2


def test_the_same_add_twice_at_once_adds_one_line(monkeypatch):
    from sqlmodel import Session, select

    from app.db import get_engine
    from app.models import OrderItem

    with _client() as alice, _client() as bob:
        _onboard(alice, "Alice", base_id="latte")
        _onboard(bob, "Bob", base_id="espresso")
        alice_id = _user_id_by_name("Alice")
        bob.post(f"/order/add/{alice_id}")
        # The second tap checked for the line before the first had added it.
        real_get = Session.get

        def blind_get(self, model, ident, *a, **k):
            if model is OrderItem:
                return None
            return real_get(self, model, ident, *a, **k)

        monkeypatch.setattr(Session, "get", blind_get)
        r = bob.post(f"/order/add/{alice_id}")
    assert r.status_code == 200
    with Session(get_engine()) as s:
        assert len(s.exec(select(OrderItem)).all()) == 1
