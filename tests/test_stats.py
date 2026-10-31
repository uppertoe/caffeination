import math
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session


# ---------------------------------------------------------------------------
# The statistical methods, against known values.
# ---------------------------------------------------------------------------


def test_association_finds_a_real_link_and_not_a_false_one():
    from app.stats import association

    linked = [("iced", "large")] * 10 + [("hot", "small")] * 10
    result = association(linked)
    assert result.p_value < 0.01
    assert result.cramers_v > 0.8
    assert result.standout in {("iced", "large"), ("hot", "small")}

    even = [("iced", "large"), ("iced", "small"), ("hot", "large"), ("hot", "small")] * 5
    result = association(even)
    assert result.p_value > 0.5
    assert result.cramers_v == pytest.approx(0.0)

    assert association([("hot", "small")] * 5) is None  # nothing to test


def test_unusualness_ranks_rare_options_above_common_ones():
    from app.stats import UnusualnessModel

    domains = {"base_id": ("latte", "piccolo", "mocha"), "milk": ("full_cream", "oat")}
    drinks = [{"base_id": "latte", "milk": "full_cream"}] * 8 + [{"base_id": "piccolo", "milk": "oat"}]
    model = UnusualnessModel(drinks, domains)
    common = model.surprisal({"base_id": "latte", "milk": "full_cream"})
    rare = model.surprisal({"base_id": "piccolo", "milk": "oat"})
    unseen = model.surprisal({"base_id": "mocha", "milk": "oat"})
    assert common < rare < unseen < math.inf
    # An option that does not apply to a drink is left out, not penalised.
    assert model.surprisal({"base_id": "latte", "milk": None}) < common


def test_holm_adjustment_matches_a_worked_example():
    from app.stats import holm_adjust

    adjusted = holm_adjust([0.01, 0.04, 0.03, 0.005])
    assert adjusted == pytest.approx([0.03, 0.06, 0.06, 0.02])


def test_even_spread_test_flags_a_real_favourite_and_not_noise():
    from app.stats import even_spread_p

    assert even_spread_p([5, 1, 4, 4, 4]) > 0.3  # 18 rounds, Monday one ahead
    assert even_spread_p([14, 1, 1, 1, 1]) < 0.01


def test_half_window_finds_the_busy_stretch_and_crosses_midnight():
    from app.stats import half_window

    # Six morning rounds and two in the afternoon: four of the eight sit in
    # the quarter hours from 9:45 to 10:15.
    times = [9 * 60 + 30, 9 * 60 + 45, 10 * 60, 10 * 60 + 15, 10 * 60, 9 * 60 + 45, 14 * 60, 14 * 60 + 30]
    assert half_window(times) == (9 * 60 + 45, 10 * 60 + 15)
    # Three of five around midnight: the window runs from 11:30 pm to 12:15 am.
    assert half_window([23 * 60 + 30, 23 * 60 + 45, 0, 12 * 60, 15 * 60]) == (23 * 60 + 30, 1440 + 15)


def test_caffeine_estimates_follow_the_drink():
    from app.menu import estimated_caffeine_mg

    assert estimated_caffeine_mg("espresso", None, "regular") == 107
    assert estimated_caffeine_mg("espresso", None, "regular", shots=2) == 214
    assert estimated_caffeine_mg("latte", "large", "extra_shot") == 321
    assert estimated_caffeine_mg("latte", "small", "decaf") < 5
    assert estimated_caffeine_mg("hot_chocolate", "large", "regular") == 5
    assert estimated_caffeine_mg("filter", "small", "regular") == 80


# ---------------------------------------------------------------------------
# The page.
# ---------------------------------------------------------------------------


def _client() -> TestClient:
    from app.main import create_app

    return TestClient(create_app())


def _onboard(client, name: str, **drink_form):
    client.get("/")
    client.post("/me/name", data={"display_name": name})
    if drink_form:
        client.post("/me/drink", data=drink_form)


def test_stats_page_before_anyone_has_a_usual():
    with _client() as me:
        r = me.get("/stats")
    assert r.status_code == 200
    assert "Department statistics" in r.text
    assert "No one in this group has saved a usual drink yet." in r.text
    assert "No rounds have been recorded yet." in r.text


def test_stats_page_shows_the_department_without_naming_anyone():
    with _client() as me, _client() as alice, _client() as bob:
        _onboard(alice, "Alice Zebedee", base_id="latte", size="small", milk="oat")
        _onboard(bob, "Bob Quixote", base_id="latte", size="small", milk="full_cream")
        _onboard(me, "Me Myself", base_id="piccolo", milk="oat")
        r = me.get("/stats")
    assert r.status_code == 200
    assert "The controls are set to your usual drink." in r.text
    assert "Shares of all 3 usual drinks." in r.text
    assert "piccolo" in r.text  # the controls start on the viewer's usual drink
    assert "bean-mine" in r.text  # and the viewer's own bean is marked
    assert "usuals" not in r.text
    for name in ("Alice", "Zebedee", "Bob", "Quixote"):
        assert name not in r.text


def test_scoring_a_drink_returns_just_the_result():
    with _client() as me, _client() as alice:
        _onboard(alice, "Alice", base_id="latte", size="small", milk="oat")
        _onboard(me, "Me", base_id="latte", size="small", milk="oat")
        r = me.get("/stats/unusual", params={"base_id": "matcha_latte", "temp": "iced", "size": "large", "milk": "soy"})
    assert r.status_code == 200
    assert 'id="unusual-result"' in r.text
    assert "large iced soy matcha latte" in r.text
    assert (
        "Its least common choice is the drink itself: none of the 2 colleagues "
        "have matcha latte as their usual drink."
    ) in r.text


def test_colleagues_filter_returns_the_body_alone():
    with _client() as me:
        _onboard(me, "Me", base_id="latte")
        r = me.get("/stats", params={"who": "all"}, headers={"HX-Request": "true", "HX-Target": "stats"})
    assert r.status_code == 200
    assert r.text.lstrip().startswith('<div id="stats"')
    assert "<html" not in r.text


def test_round_charts_appear_from_five_rounds():
    from app.db import get_engine
    from app.models import Round

    with _client() as me:
        _onboard(me, "Me", base_id="latte")
        r = me.get("/stats")
        assert "The round charts will appear once 5 rounds have been recorded." in r.text
        with Session(get_engine()) as s:
            base = datetime(2026, 10, 26, 23, 0, tzinfo=timezone.utc)  # Tuesday 10 am in Melbourne
            for i in range(5):
                s.add(Round(placed_at=base + timedelta(days=i % 2, minutes=15 * i), drink_count=4))
            s.commit()
        r = me.get("/stats")
    assert "Time of day" in r.text and "Day of the week" in r.text
    assert "Drinks in a typical round:" in r.text
    assert "Busiest stretch, holding half of all rounds:" in r.text
    # 23:00 UTC on Monday 26 October is 10 am on Tuesday in Melbourne (AEDT),
    # and three of the five rounds fall on that local day.
    import re

    days = dict((d, int(c)) for c, d in re.findall(
        r'class="day-count">(\d+)</span>\s*<span class="day-bar"[^>]*></span>\s*<span class="day-name">(\w+)<', r.text))
    assert days == {"Monday": 0, "Tuesday": 3, "Wednesday": 2, "Thursday": 0, "Friday": 0}
    # Five rounds are too few for any day to stand out beyond chance.
    assert "No day stands out yet" in r.text


def test_order_page_links_to_the_statistics():
    with _client() as me:
        _onboard(me, "Me", base_id="latte")
        r = me.get("/")
    assert "counted in the department statistics, which never name anyone" in r.text
    assert 'href="/stats"' in r.text


def test_stats_page_query_count_does_not_grow_with_the_department():
    def queries(n_colleagues):
        with _client() as me:
            _onboard(me, "Me", base_id="latte")
            for i in range(n_colleagues):
                with _client() as other:
                    _onboard(other, f"Colleague {n_colleagues}-{i}", base_id="flat_white", size="small", milk="oat")
            r = me.get("/stats")
        return int(r.headers["server-timing"].split('desc="')[1].split()[0])

    assert queries(2) == queries(8) <= 6


def test_trying_a_drink_never_says_whether_a_colleague_has_it():
    # Saying "no one else has this exact drink" would let anyone test a
    # guess about a colleague's order, so neither the page nor the scoring
    # fragment mentions exact matches.
    with _client() as me, _client() as alice:
        _onboard(alice, "Alice", base_id="mocha", size="large", milk="soy")
        _onboard(me, "Me", base_id="latte", size="small", milk="oat")
        page = me.get("/stats")
        probe = me.get("/stats/unusual", params={"base_id": "mocha", "size": "large", "milk": "soy"})
    panel = page.text[page.text.index('class="unusual"'):page.text.index('class="caffeine"')]
    for text in (panel, probe.text):
        assert "exact" not in text.lower()
        assert "no one else" not in text.lower()


def test_the_reason_names_the_option_that_makes_a_drink_unusual():
    with _client() as me, _client() as a, _client() as b:
        _onboard(a, "A", base_id="latte", size="small", milk="full_cream")
        _onboard(b, "B", base_id="latte", size="small", milk="full_cream")
        _onboard(me, "Me", base_id="latte", size="small", milk="lactose_free")
        r = me.get("/stats")
    assert "Its least common option is lactose-free milk: 1 of the 3 usual drinks made with milk uses it." in r.text


def test_someone_without_a_drink_starts_on_the_most_common_one():
    with _client() as me, _client() as a, _client() as b:
        _onboard(a, "A", base_id="cappuccino", size="small", milk="full_cream")
        _onboard(b, "B", base_id="cappuccino", size="small", milk="full_cream")
        _onboard(me, "Me")
        r = me.get("/stats")
    assert "The controls are set to the department’s most common usual drink." in r.text
    assert '<p class="unusual-line">cappuccino</p>' in r.text


def test_someone_without_a_drink_sees_the_page_when_no_order_leads():
    with _client() as me, _client() as a, _client() as b:
        _onboard(a, "A", base_id="cappuccino", size="small", milk="full_cream")
        _onboard(b, "B", base_id="latte", size="small", milk="oat")
        _onboard(me, "Me")
        r = me.get("/stats")
    assert r.status_code == 200
    assert "No single exact order leads yet" in r.text
    assert "The controls are set to the department’s most common usual drink." in r.text


def test_caffeine_wording_and_rows_agree_in_half_shots():
    from app.stats_view import CAFFEINE_ROWS, describe_mg, half_shots

    # 40 mg and 54 mg are both about half a shot; neither is "about one shot".
    assert describe_mg(40) == describe_mg(54) == "about half a shot"
    assert describe_mg(107) == "about one shot"
    assert describe_mg(160) == "about one and a half shots"
    for mg in (0, 5, 40, 54, 80, 107, 160, 214, 321):
        assert describe_mg(mg) == CAFFEINE_ROWS[half_shots(mg)].lower()
