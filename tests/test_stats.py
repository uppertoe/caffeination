import math
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select


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

    assert estimated_caffeine_mg("espresso", None, "regular") == 106
    assert estimated_caffeine_mg("espresso", None, "regular", shots=2) == 212
    assert estimated_caffeine_mg("latte", "large", "extra_shot") == 318
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
    assert "<td>Matcha latte</td>" in r.text and "<td>0 of 2 usual drinks</td>" in r.text


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

    days = dict((d, int(c)) for d, c in re.findall(r'data-tip="(\w+day): (\d+) rounds?,', r.text))
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


def test_the_working_shows_what_each_option_adds_to_the_score():
    import re

    with _client() as me, _client() as a, _client() as b:
        _onboard(a, "A", base_id="latte", size="small", milk="full_cream")
        _onboard(b, "B", base_id="latte", size="small", milk="full_cream")
        _onboard(me, "Me", base_id="latte", size="small", milk="lactose_free")
        r = me.get("/stats")
    rows = re.findall(r'<tr class="([^"]*)">\s*<th scope="row">(\w+)</th>(.*?)</tr>', r.text, re.S)
    by_label = {label: (cls, body) for cls, label, body in rows}
    cls, body = by_label["Milk"]
    assert cls == "is-rarest" and "Lactose-free" in body and "1 of 3 made with milk" in body
    assert by_label["Shots"][0] == "is-na" and "Does not apply" in by_label["Shots"][1]
    # Milk is 1 of 3 with six milks and half a drink each: −log₂(1.5 / 6) = 2 bits.
    assert ">2.0</td>" in body
    assert "This drink scores" in r.text and "bits</strong>, as surprising as a 1 in" in r.text


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


# ---------------------------------------------------------------------------
# The method panels.
# ---------------------------------------------------------------------------


def test_chi2_tail_matches_tables():
    from app.stats import chi2_sf

    assert chi2_sf(3.841459, 1) == pytest.approx(0.05, abs=1e-6)
    assert chi2_sf(9.487729, 4) == pytest.approx(0.05, abs=1e-6)
    assert chi2_sf(2.0, 10) == pytest.approx(0.99634, abs=1e-5)
    assert chi2_sf(0.0, 3) == 1.0


def test_chi_square_working_adds_up():
    from app.stats import chi_square

    pairs = [("oat", "large")] * 6 + [("oat", "small")] * 2 + [("dairy", "large")] * 3 + [("dairy", "small")] * 9
    t = chi_square(pairs)
    cells = [(i, j) for i in range(2) for j in range(2)]
    assert sum(t.contribution(i, j) for i, j in cells) == pytest.approx(t.chi2)
    assert sum(sum(row) for row in t.expected) == pytest.approx(t.n)
    assert t.df == 1 and len(t.null) == 499
    assert t.p_permutation == pytest.approx((t.as_extreme + 1) / 500)
    assert t.sparse_cells == 2  # oat-large 3.6 and oat-small 4.4


def test_random_allocations_give_evenly_spread_baseline_p_values():
    import random

    from app.stats import randomised_baseline_ps

    rng = random.Random(3)
    values = [rng.random() for _ in range(60)]
    groups = [rng.choice("abc") for _ in range(60)]
    ps = randomised_baseline_ps([(values, "number"), (groups, "category")], randomisations=600)
    for j in range(2):
        below = sum(p[j] < 0.05 for p in ps) / len(ps)
        assert 0.02 < below < 0.09


def test_one_cafe_rounds_are_far_less_certain_than_mixed_ones():
    from app.stats import simulate_caffeine

    sims = simulate_caffeine([(1.0, 0.0)] * 30 + [(0.0, 35.0)] * 4, 106, 38)
    one, mixed = sims["one_cafe"], sims["separate"]
    assert one.estimate == mixed.estimate == 30 * 106 + 140
    assert one.high - one.low > 3 * (mixed.high - mixed.low)
    assert one.low < one.estimate < one.high


def test_power_calculations_match_textbook_values():
    from app.stats import n2_for_power, n_per_group_for_means, power_two_proportions

    # Two groups of 100 at 50% against 70%: about 83% power.
    assert power_two_proportions(0.5, 0.7, 100, 100) == pytest.approx(0.83, abs=0.02)
    assert n_per_group_for_means(0.5, 1.0) == 63
    n2 = n2_for_power(0.3, 0.5, 200)
    assert power_two_proportions(0.3, 0.5, 200, n2) >= 0.8 > power_two_proportions(0.3, 0.5, 200, n2 - 1)
    # A first group too small for any second group to reach 80% power.
    assert n2_for_power(0.3, 0.5, 20) is None


def test_method_panels_render_and_reallocate():
    with _client() as me:
        _onboard(me, "Me", base_id="latte", size="small", milk="oat")
        for i, (base, milk, size) in enumerate(
            [("latte", "full_cream", "small"), ("flat_white", "oat", "large"), ("cappuccino", "soy", "regular"),
             ("latte", "full_cream", "large"), ("chai_latte", "oat", "small"), ("mocha", "full_cream", "regular")] * 3
        ):
            with _client() as c:
                _onboard(c, f"P{i}", base_id=base, milk=milk, size=size)
        page = me.get("/stats")
        again = me.get("/stats/randomise", params={"allocation": 2})
    assert "Options that go together" in page.text
    assert "χ² = " in page.text and "shuffled tables had a χ² at least as large" in page.text
    assert "Table 1, allocation 1" in page.text and "Across 1,000 random allocations" in page.text
    assert "A round for the whole department" in page.text
    assert "This comparison needs at least 10 colleagues who have moved on, and 0 have so far." in page.text
    assert again.status_code == 200 and "Table 1, allocation 2" in again.text
    assert "allocation=3" in again.text


def test_order_page_offers_the_statistics():
    with _client() as me:
        _onboard(me, "Me", base_id="latte")
        r = me.get("/")
    assert 'class="stats-teaser"' in r.text and 'href="/stats"' in r.text


# ---------------------------------------------------------------------------
# The round-log methods and panels.
# ---------------------------------------------------------------------------


def test_round_methods_match_reference_values():
    from app.stats import fisher_exact, fragility_index, kaplan_meier, km_median, lorenz, t_sf, welch, wilson

    assert t_sf(2.228139, 10) == pytest.approx(0.025, abs=1e-5)
    w = welch([1, 2, 3, 4, 5], [2, 3, 4, 5, 9])  # R: t.test gives p = 0.2937, df = 6.452
    assert w.p == pytest.approx(0.2937, abs=1e-4) and w.df == pytest.approx(6.452, abs=1e-3)
    assert fisher_exact(1, 9, 11, 3) == pytest.approx(0.002759, abs=1e-6)  # R: fisher.test
    assert fisher_exact(3, 1, 1, 3) == pytest.approx(0.4857, abs=1e-4)
    assert fragility_index(1, 9, 11, 3) == 3
    assert fragility_index(3, 1, 1, 3) is None
    lo, hi = wilson(5, 20)
    assert lo == pytest.approx(0.1119, abs=1e-4) and hi == pytest.approx(0.4687, abs=1e-4)
    steps = kaplan_meier([1, 2, 2, 3, 4, 5, 6, 7, 8, 9], [1, 1, 0, 1, 1, 0, 1, 1, 1, 0])
    assert [round(s.survival, 3) for s in steps] == [1.0, 0.9, 0.8, 0.686, 0.571, 0.429, 0.286, 0.143]
    assert km_median(steps)[0] == 6
    assert lorenz([0, 0, 0, 4])[1] == pytest.approx(0.75)


def test_mantel_haenszel_removes_confounding():
    from app.stats import mantel_haenszel

    # Within each stratum the odds ratio is exactly 1, but the exposed sit
    # mostly in the stratum with more outcomes, so the crude ratio is not.
    crude, adjusted = mantel_haenszel([(40, 10, 8, 2), (2, 8, 10, 40)])
    assert adjusted.estimate == pytest.approx(1.0)
    assert crude.estimate > 3


def _log_rounds(n: int, guests: int = 0):
    """n rounds of the roster's usual drinks, every weekday morning and
    afternoon, with a guest in the first few."""
    from app.db import get_engine
    from app.models import Round, RoundDrink, SavedDrink, User

    with Session(get_engine()) as s:
        people = s.exec(select(User.id)).all()
        base = datetime(2026, 9, 1, 23, 0, tzinfo=timezone.utc)
        for i in range(n):
            placed = base + timedelta(days=i // 2, hours=4 * (i % 2))
            rnd = Round(placed_at=placed, drink_count=0)
            s.add(rnd)
            s.flush()
            chosen = [p for j, p in enumerate(people) if (i + j) % 3]
            for uid in chosen:
                sd = s.get(SavedDrink, uid)
                s.add(RoundDrink(round_id=rnd.id, user_id=uid, base_id=sd.base_id, temp=sd.temp,
                                 size=sd.size, milk=sd.milk, strength=sd.strength, sweetener=sd.sweetener))
            if i < guests:
                s.add(RoundDrink(round_id=rnd.id, guest=True, base_id="hot_chocolate", temp="hot",
                                 size="regular", milk="full_cream", strength="regular", sweetener="none"))
            rnd.drink_count = len(chosen) + (i < guests)
        s.commit()



def test_round_panels_wait_then_appear():
    with _client() as me:
        _onboard(me, "Me", base_id="latte", size="small", milk="full_cream")
        for i, base in enumerate(["flat_white", "long_black", "cappuccino", "chai_latte", "piccolo", "espresso"]):
            with _client() as c:
                _onboard(c, f"P{i}", base_id=base, size="regular", milk="oat")
        waiting = me.get("/stats").text
        _log_rounds(40, guests=6)
        ready = me.get("/stats").text
    assert "This comparison needs at least 20 drinks either side of midday" in waiting
    assert "This curve needs at least 15 return visits" in waiting
    assert "Two intervals for the same difference" in ready
    assert "Mantel–Haenszel odds ratio, within drink groups:" in ready
    assert "of the department’s return visits" in ready or "median time is still unknown" in ready
    assert "account for half of the drinks in the rounds" in ready
    assert "Rounds you have been in:" in ready
    assert "Regression to the mean" in ready and "A CUSUM chart" in ready
    assert "Fisher’s exact p-value:" in ready
    # Nothing on the page names a colleague.
    for name in ("P0", "P1", "P2", "P3", "P4", "P5"):
        assert f">{name}<" not in ready and f" {name} " not in ready


def test_ausnut_reference_and_agreement():
    from app.menu import ausnut_caffeine_mg
    from app.stats import bland_altman, cohen_kappa

    assert ausnut_caffeine_mg("latte", "small", "regular") == 143
    assert ausnut_caffeine_mg("latte", "large", "regular") == 291
    assert ausnut_caffeine_mg("espresso", None, "regular", 2) == 232
    assert ausnut_caffeine_mg("filter", "small", "regular") is None
    assert ausnut_caffeine_mg("latte", "small", "decaf") is None
    # A method that reads 30% high correlates perfectly but does not agree.
    ba = bland_altman([1, 2, 3, 4, 5, 6], [1.3, 2.6, 3.9, 5.2, 6.5, 7.8])
    assert ba.r == pytest.approx(1.0) and ba.bias == pytest.approx(1.05) and ba.slope == pytest.approx(0.2609, abs=1e-4)
    # Worked by hand: observed 6/8, chance 23/64.
    assert cohen_kappa([0, 0, 1, 1, 2, 2, 0, 1], [0, 1, 1, 1, 2, 0, 0, 1], 3) == pytest.approx(0.6098, abs=1e-4)
    assert cohen_kappa([0, 0, 1, 1, 2, 2, 0, 1], [0, 1, 1, 1, 2, 0, 0, 1], 3, weighted=True) == pytest.approx(0.52)
