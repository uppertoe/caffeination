from fastapi.testclient import TestClient
from sqlmodel import Session, select


def _client() -> TestClient:
    from app.main import create_app

    return TestClient(create_app())


# ---------------------------------------------------------------------------
# Unit tests on the formatter (no HTTP).
# ---------------------------------------------------------------------------


def _make_saved(**overrides):
    from app.models import SavedDrink

    defaults = dict(
        user_id="u1",
        base_id="latte",
        temp="hot",
        size="small",
        milk="full_cream",
        strength="regular",
        sweetener="none",
        length=None,
        notes=None,
    )
    defaults.update(overrides)
    return SavedDrink(**defaults)


def test_format_default_latte_omits_defaults():
    from app.drinks import format_drink
    from app.menu import get_drink

    # small is the default size, so it is not spelled out.
    line = format_drink(get_drink("latte"), _make_saved())
    assert line == "latte"


def test_format_regular_size_is_spelled_out():
    from app.drinks import format_drink
    from app.menu import get_drink

    assert format_drink(get_drink("latte"), _make_saved(size="regular")) == "regular latte"


def test_format_large_oat_flat_white():
    from app.drinks import format_drink
    from app.menu import get_drink

    sd = _make_saved(base_id="flat_white", size="large", milk="oat")
    assert format_drink(get_drink("flat_white"), sd) == "large oat flat white"


def test_format_espresso():
    from app.drinks import format_drink
    from app.menu import get_drink

    sd = _make_saved(base_id="espresso", size=None, milk=None)
    assert format_drink(get_drink("espresso"), sd) == "espresso"


def test_format_macchiato_long_short():
    from app.drinks import format_drink
    from app.menu import get_drink

    short = _make_saved(base_id="macchiato", size=None, milk=None, length="short")
    long_ = _make_saved(base_id="macchiato", size=None, milk=None, length="long")
    assert format_drink(get_drink("macchiato"), short) == "short macchiato"
    assert format_drink(get_drink("macchiato"), long_) == "long macchiato"


def test_format_iced_soy_mocha_with_extras():
    from app.drinks import format_drink
    from app.menu import get_drink

    sd = _make_saved(
        base_id="mocha",
        temp="iced",
        size="small",
        milk="soy",
        sweetener="one_sugar",
        notes="light ice",
    )
    assert (
        format_drink(get_drink("mocha"), sd)
        == "iced soy mocha (1 sugar, light ice)"
    )


def test_format_lowercases_notes():
    """Free-text notes join the line lowercased so the till summary stays
    uniformly lowercase whatever the user typed."""
    from app.drinks import format_drink
    from app.menu import get_drink

    sd = _make_saved(base_id="latte", notes="EXTRA Hot Please")
    assert format_drink(get_drink("latte"), sd) == "latte (extra hot please)"


def test_format_magic_is_just_magic():
    """Magic is fixed-size, fixed-shots, so the line drops all axes."""
    from app.drinks import format_drink
    from app.menu import get_drink

    sd = _make_saved(base_id="magic", size=None, milk="full_cream")
    assert format_drink(get_drink("magic"), sd) == "magic"


# ---------------------------------------------------------------------------
# Normalisation drops fields that don't apply to the chosen base.
# ---------------------------------------------------------------------------


def test_normalize_strips_size_for_espresso():
    from app.drinks import normalize

    out = normalize("espresso", {"size": "large", "milk": "oat"})
    assert out["size"] is None
    assert out["milk"] is None
    assert out["shots"] == 1  # espresso defaults to a single


def test_espresso_takes_a_single_or_double_and_nothing_else_does():
    from app.drinks import format_line, normalize

    double = normalize("espresso", {"shots": "2"})
    assert double["shots"] == 2
    assert normalize("espresso", {"shots": "3"})["shots"] == 1
    assert normalize("latte", {"shots": "2"})["shots"] is None
    assert format_line("espresso", None, None, "hot", "regular", None, "none", None, 2) == "double espresso"
    assert format_line("espresso", None, None, "hot", "regular", None, "none", None, 1) == "espresso"


def test_extra_hot_applies_only_to_drinks_made_with_milk():
    from app.drinks import format_line, normalize

    latte = normalize("latte", {"temp": "extra_hot", "size": "small"})
    assert latte["temp"] == "extra_hot"
    assert format_line("latte", None, "small", "extra_hot", "regular", "oat", "none", None) == "extra hot oat latte"
    assert normalize("long_black", {"temp": "extra_hot"})["temp"] == "hot"
    assert normalize("hot_chocolate", {"temp": "extra_hot"})["temp"] == "extra_hot"


def test_own_cup_is_noted_on_the_line():
    from app.drinks import format_line, normalize

    assert normalize("latte", {"own_cup": "1"})["own_cup"] is True
    assert normalize("latte", {})["own_cup"] is False
    line = format_line("latte", None, "small", "hot", "regular", "full_cream", "one_sugar", None, None, True)
    assert line == "latte (1 sugar, own cup)"


def test_filter_and_cold_brew_are_served_black():
    from app.drinks import format_line, normalize

    filt = normalize("filter", {"size": "large", "milk": "oat", "temp": "iced"})
    assert (filt["size"], filt["milk"], filt["temp"]) == ("large", None, "hot")
    assert format_line("filter", None, "large", "hot", "regular", None, "none", None) == "large filter"
    cold = normalize("cold_brew", {"size": "large", "temp": "extra_hot"})
    assert (cold["size"], cold["temp"]) == (None, "hot")
    assert format_line("cold_brew", None, None, "hot", "regular", None, "none", None) == "cold brew"


def test_normalize_macchiato_has_length_no_size():
    from app.drinks import normalize

    out = normalize("macchiato", {"length": "long"})
    assert out["length"] == "long"
    assert out["size"] is None


def test_normalize_unknown_drink_returns_none():
    from app.drinks import normalize

    assert normalize("frappe-mochiatto", {}) is None


# ---------------------------------------------------------------------------
# HTTP end-to-end through the form.
# ---------------------------------------------------------------------------


def _seed_named_user(client, name="Sam"):
    client.get("/")
    r = client.post("/me/name", data={"display_name": name})
    assert r.status_code == 200


def test_initial_dashboard_offers_to_set_drink():
    with _client() as client:
        _seed_named_user(client)
        r = client.get("/")
    assert "Set up your drink" in r.text
    assert "Your usual" in r.text


def test_save_drink_then_card_shows_till_line():
    with _client() as client:
        _seed_named_user(client)
        r = client.post(
            "/me/drink",
            data={
                "base_id": "flat_white",
                "size": "large",
                "milk": "oat",
                "strength": "regular",
                "sweetener": "none",
                "temp": "hot",
                "notes": "",
            },
        )
    assert r.status_code == 200
    assert "large oat flat white" in r.text
    assert "Edit drink" in r.text


def test_get_edit_form_after_saved_drink_shows_form():
    with _client() as client:
        _seed_named_user(client)
        client.post(
            "/me/drink",
            data={"base_id": "latte", "size": "regular", "milk": "full_cream"},
        )
        r = client.get("/me/drink/edit")
    assert r.status_code == 200
    assert "Base drink" in r.text
    assert 'name="base_id"' in r.text


def test_cancel_returns_card_without_persisting_form_values():
    with _client() as client:
        _seed_named_user(client)
        # Save a known drink first.
        client.post(
            "/me/drink",
            data={"base_id": "espresso", "notes": "double shot"},
        )
        # Cancel returns the existing card, unchanged.
        r = client.get("/me/drink")
    assert r.status_code == 200
    assert "espresso (double shot)" in r.text


def test_init_db_adds_the_new_columns_to_an_existing_database():
    import sqlite3

    from app.config import get_settings
    from app.db import init_db

    path = get_settings().database_url.removeprefix("sqlite:///")
    con = sqlite3.connect(path)
    con.executescript(
        'CREATE TABLE "user" (id VARCHAR PRIMARY KEY, display_name VARCHAR, created_at TIMESTAMP,'
        " created_by VARCHAR, one_off BOOLEAN);"
        "CREATE TABLE saveddrink (user_id VARCHAR PRIMARY KEY, base_id VARCHAR, temp VARCHAR,"
        " size VARCHAR, milk VARCHAR, strength VARCHAR, sweetener VARCHAR, length VARCHAR,"
        " notes VARCHAR, updated_at TIMESTAMP);"
        "INSERT INTO \"user\" VALUES ('u1', 'Old Timer', '2026-01-01', NULL, 0);"
        "INSERT INTO saveddrink VALUES ('u1', 'espresso', 'hot', NULL, NULL, 'regular', 'none', NULL, 'Double', '2026-01-01');"
        # The round log as it first shipped, without links to people.
        "CREATE TABLE round (id INTEGER PRIMARY KEY, placed_at TIMESTAMP, drink_count INTEGER);"
        "CREATE TABLE rounddrink (id INTEGER PRIMARY KEY, round_id INTEGER, base_id VARCHAR,"
        " temp VARCHAR, size VARCHAR, milk VARCHAR, strength VARCHAR, sweetener VARCHAR,"
        " length VARCHAR, shots INTEGER, has_notes BOOLEAN, guest BOOLEAN);"
    )
    con.commit()
    con.close()

    init_db()

    con = sqlite3.connect(path)
    cols = {row[1] for row in con.execute("PRAGMA table_info(saveddrink)")}
    row = con.execute("SELECT shots, own_cup, notes FROM saveddrink").fetchone()
    round_cols = {row[1] for row in con.execute("PRAGMA table_info(round)")}
    drink_cols = {row[1] for row in con.execute("PRAGMA table_info(rounddrink)")}
    history = con.execute("SELECT user_id, base_id, changed_at FROM drinkchange").fetchall()
    con.close()
    assert {"shots", "own_cup"} <= cols
    assert row == (None, 0, "Double")
    assert "owner_id" in round_cols and {"user_id", "own_cup"} <= drink_cols
    # The drink as it stood seeds the history, once.
    assert history == [("u1", "espresso", "2026-01-01")]
    init_db()
    con = sqlite3.connect(path)
    assert con.execute("SELECT count(*) FROM drinkchange").fetchone() == (1,)
    con.close()


def _uid(name: str) -> str:
    from app.db import get_engine
    from app.models import User

    with Session(get_engine()) as s:
        return s.exec(select(User).where(User.display_name == name)).first().id


def _history(user_id: str) -> list:
    from app.db import get_engine
    from app.models import DrinkChange

    with Session(get_engine()) as s:
        rows = s.exec(select(DrinkChange).where(DrinkChange.user_id == user_id).order_by(DrinkChange.id))
        return [(r.base_id, r.milk, r.changed_by) for r in rows]


def test_drink_history_records_each_change_and_who_made_it():
    with _client() as me, _client() as other:
        me.get("/")
        me.post("/me/name", data={"display_name": "Me"})
        me.post("/me/drink", data={"base_id": "latte", "size": "small", "milk": "full_cream"})
        me.post("/me/drink", data={"base_id": "latte", "size": "small", "milk": "full_cream"})
        me.post("/me/drink", data={"base_id": "latte", "size": "small", "milk": "oat"})
        me_id = _uid("Me")
        other.get("/")
        other.post("/me/name", data={"display_name": "Other"})
        other.post("/people", data={"display_name": "Newbie", "base_id": "piccolo", "milk": "soy"})
        other.post("/people", data={"display_name": "Visitor", "base_id": "mocha", "one_off": "1"})
        other_id = _uid("Other")
    assert _history(me_id) == [("latte", "full_cream", None), ("latte", "oat", None)]
    assert _history(_uid("Newbie")) == [("piccolo", "soy", other_id)]
    assert _history(_uid("Visitor")) == []
