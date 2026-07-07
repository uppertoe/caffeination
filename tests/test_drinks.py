from fastapi.testclient import TestClient


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
    assert "shots" not in out  # shots is no longer a structured axis


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
        r = client.get("/me/drink/cancel")
    assert r.status_code == 200
    assert "espresso (double shot)" in r.text
