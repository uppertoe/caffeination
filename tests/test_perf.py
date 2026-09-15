"""Guards on the per-request cost, read from the Server-Timing header the
app stamps on every response. The point is not the millisecond figure (that
depends on the machine) but that the statement count stays flat as the
roster and the order grow — a regression to per-row queries shows up here
as a number, not as a slow page months later."""

import gzip
import re

from fastapi.testclient import TestClient


def _client() -> TestClient:
    from app.main import create_app

    return TestClient(create_app())


def _onboard(client, name: str, **drink_form):
    client.get("/")
    client.post("/me/name", data={"display_name": name})
    if drink_form:
        client.post("/me/drink", data=drink_form)


def _user_id_by_name(name: str) -> str:
    from sqlmodel import Session, select

    from app.db import get_engine
    from app.models import User

    with Session(get_engine()) as s:
        return s.exec(select(User).where(User.display_name == name)).first().id


def _queries(response) -> int:
    header = response.headers["server-timing"]
    return int(re.search(r'db;desc="(\d+) queries"', header).group(1))


def test_server_timing_header_present():
    with _client() as c:
        r = c.get("/healthz")
    assert re.fullmatch(r'app;dur=\d+\.\d, db;desc="\d+ queries"', r.headers["server-timing"])


def test_dashboard_query_count_does_not_grow_with_roster_or_order():
    with _client() as me:
        _onboard(me, "Me", base_id="latte")
        small = None
        for n in range(12):
            with _client() as other:
                _onboard(other, f"Colleague {n}", base_id="flat_white", size="large", milk="oat")
            me.post(f"/order/add/{_user_id_by_name(f'Colleague {n}')}")
            if n == 2:
                small = _queries(me.get("/"))
        large = _queries(me.get("/"))
    # Three colleagues vs twelve, all in the order and none left on the
    # roster: same number of statements either way, and few of them.
    assert small == large
    assert large <= 6, large


def test_remove_and_clear_are_set_based():
    with _client() as me:
        _onboard(me, "Me", base_id="latte")
        for n in range(6):
            with _client() as other:
                _onboard(other, f"Colleague {n}", base_id="espresso")
            me.post(f"/order/add/{_user_id_by_name(f'Colleague {n}')}")
        for n in range(3):
            me.post("/people", data={"display_name": f"Guest {n}", "base_id": "latte", "one_off": "1"})
        removed = me.post(f"/order/remove/{_user_id_by_name('Colleague 0')}")
        cleared = me.post("/order/clear")
    assert _queries(removed) <= 8, _queries(removed)
    assert _queries(cleared) <= 8, _queries(cleared)
    assert "Guest" not in cleared.text


def test_html_is_gzipped_but_fonts_are_not():
    with _client() as c:
        page = c.get("/", headers={"Accept-Encoding": "gzip"})
        assert page.headers.get("content-encoding") == "gzip"
        assert "<!doctype html>" in page.text.lower()  # httpx transparently decodes

        font = c.get("/static/vendor/shrikhand-latin.woff2", headers={"Accept-Encoding": "gzip"})
        assert font.status_code == 200
        assert "content-encoding" not in font.headers

        plain = c.get("/", headers={"Accept-Encoding": "identity"})
        assert "content-encoding" not in plain.headers


def test_identity_cookie_still_set_through_asgi_middleware():
    from app.config import get_settings

    with _client() as c:
        r = c.get("/")
    cookie = r.headers["set-cookie"]
    assert f"{get_settings().cookie_name}=" in cookie
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie
    assert "Secure" not in cookie  # http://testserver


def test_roster_line_uses_memoised_formatter_consistently():
    """Two people with the same usual must render the same line, and the
    till summary must still collapse them."""
    with _client() as me, _client() as a, _client() as b:
        _onboard(me, "Me", base_id="espresso")
        _onboard(a, "Ann", base_id="latte", size="large", milk="oat", notes="")
        _onboard(b, "Ben", base_id="latte", size="large", milk="oat", notes="")
        me.post(f"/order/add/{_user_id_by_name('Ann')}")
        r = me.post(f"/order/add/{_user_id_by_name('Ben')}")
    assert "2x large oat latte" in r.text
