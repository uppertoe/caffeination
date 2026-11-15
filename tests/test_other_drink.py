"""The "Something else" drink: a drink the person names in free text."""

from fastapi.testclient import TestClient


def _client() -> TestClient:
    from app.main import create_app

    return TestClient(create_app())


def _onboard(client, name: str):
    client.get("/")
    client.post("/me/name", data={"display_name": name})


OTHER = {"base_id": "other", "size": "large", "milk": "oat", "sweetener": "two_sugar",
         "temp": "iced", "strength": "decaf", "shots": "2"}


def test_normalize_keeps_only_the_name():
    from app.drinks import normalize

    out = normalize("other", {**OTHER, "drink_name": "  Coke   Zero ", "notes": "no ice"})
    assert out["drink_name"] == "Coke Zero"
    assert out["notes"] == "no ice"
    # Nothing else applies to a drink the person names.
    assert out["size"] is None and out["milk"] is None and out["shots"] is None
    assert out["temp"] == "hot" and out["strength"] == "regular" and out["sweetener"] == "none"
    # Other drinks never carry a name, even when the form sends one.
    assert normalize("latte", {"drink_name": "Coke Zero"})["drink_name"] is None


def test_blank_name_is_refused_with_its_own_message():
    from app.drinks import drink_error, normalize

    form = {"base_id": "other", "drink_name": "   "}
    assert normalize("other", form) is None
    assert drink_error(form, "Unknown drink.") == "Enter the name of the drink."
    assert drink_error({"base_id": "nope"}, "Unknown drink.") == "Unknown drink."


def test_name_is_capped_at_forty_characters():
    from app.drinks import normalize

    assert len(normalize("other", {"drink_name": "x" * 60})["drink_name"]) == 40


def test_till_line_is_the_name_as_typed():
    from app.drinks import format_line

    assert format_line("other", None, None, "hot", "regular", None, "none", None,
                       None, False, "Coke Zero") == "Coke Zero"
    assert format_line("other", None, None, "hot", "regular", None, "none", "no ice",
                       None, True, "Coke Zero") == "Coke Zero (own cup, no ice)"
    # Without a name (the statistics keep none) it is the menu's label.
    assert format_line("other", None, None, "hot", "regular", None, "none", None) == "something else"


def test_save_usual_and_see_it_on_the_card():
    with _client() as c:
        _onboard(c, "Alice")
        r = c.post("/me/drink", data={"base_id": "other", "drink_name": "Peppermint tea"})
        assert r.status_code == 200
        assert "Peppermint tea" in r.text
        r = c.get("/")
        assert "Peppermint tea" in r.text


def test_blank_name_on_your_usual_keeps_something_else_chosen():
    with _client() as c:
        _onboard(c, "Alice")
        c.post("/me/drink", data={"base_id": "latte"})
        r = c.post("/me/drink", data={"base_id": "other", "drink_name": ""})
    assert r.status_code == 422
    assert "Enter the name of the drink." in r.text
    assert 'baseId: "other"' in r.text


def test_blank_name_when_adding_someone():
    with _client() as c:
        _onboard(c, "Bob")
        r = c.post("/people", data={"display_name": "Priya", "base_id": "other", "drink_name": " "})
    assert r.status_code == 422
    assert "Enter the name of the drink." in r.text
    assert 'baseId: "other"' in r.text


def test_same_name_in_any_case_is_one_till_line():
    with _client() as c:
        _onboard(c, "Bob")
        c.post("/me/drink", data={"base_id": "latte"})
        for name, drink in (("Priya", "Coke Zero"), ("Tom", "coke zero"), ("Dana", "Lemonade")):
            r = c.post("/people", data={"display_name": name, "base_id": "other", "drink_name": drink})
            assert r.status_code == 200
        r = c.get("/order")
    assert "2x Coke Zero" in r.text
    assert "1x Lemonade" in r.text


def test_the_name_survives_closing_joins():
    """Closing joins copies each usual onto its line, so a later change to
    the usual doesn't alter what was ordered: the name must come too."""
    from app.drinks import DRINK_OPTION_FIELDS, HISTORY_FIELDS, LINE_FIELDS

    assert "drink_name" in DRINK_OPTION_FIELDS
    assert "drink_name" in HISTORY_FIELDS
    assert "drink_name" in LINE_FIELDS


def test_statistics_leave_free_text_drinks_out():
    from app.stats_view import DOMAINS, DRINK_LABELS
    from app.menu import caffeine_parts

    assert "other" not in DOMAINS["base_id"]
    assert "other" not in DRINK_LABELS
    assert caffeine_parts("other", None, "regular") == (0.0, 0.0)
    with _client() as c:
        _onboard(c, "Alice")
        c.post("/me/drink", data={"base_id": "other", "drink_name": "Kombucha"})
        r = c.get("/stats")
    assert r.status_code == 200
    assert "Kombucha" not in r.text
    assert "Something else" not in r.text
