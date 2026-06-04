def _row(display_name, **overrides):
    from app.menu import get_drink
    from app.drinks import format_drink
    from app.models import SavedDrink, User
    from app.orders import OrderRow

    defaults = dict(
        user_id=display_name.lower(),
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
    sd = SavedDrink(**defaults)
    user = User(id=defaults["user_id"], display_name=display_name)
    drink = get_drink(sd.base_id)
    line = format_drink(drink, sd)
    return OrderRow(user=user, saved=sd, line=line, is_self=False)


def test_summary_collapses_two_identical_drinks():
    from app.orders import till_summary

    a = _row("Alice", base_id="latte")
    b = _row("Bob", base_id="latte")
    assert till_summary([a, b]) == ["2x latte"]


def test_summary_does_not_collapse_when_size_differs():
    from app.orders import till_summary

    a = _row("Alice", base_id="latte", size="small")
    b = _row("Bob", base_id="latte", size="large")
    # small is the default and isn't spelled out; large is.
    assert sorted(till_summary([a, b])) == sorted(["1x latte", "1x large latte"])


def test_summary_does_not_collapse_when_milk_differs():
    from app.orders import till_summary

    a = _row("Alice", base_id="latte", milk="oat")
    b = _row("Bob", base_id="latte", milk="full_cream")
    assert sorted(till_summary([a, b])) == sorted(["1x latte", "1x oat latte"])


def test_summary_keeps_notes_drinks_standalone():
    from app.orders import till_summary

    a = _row("Alice", base_id="latte")
    b = _row("Bob", base_id="latte", notes="extra hot")
    c = _row("Cat", base_id="latte", notes="extra hot")
    # a and b share base/options but b has notes -> b stays standalone.
    # b and c share notes but notes never merge.
    out = till_summary([a, b, c])
    assert "1x latte" in out  # alice
    # b and c are both "1x latte (extra hot)" standalone entries
    assert out.count("1x latte (extra hot)") == 2


def test_summary_skips_rows_without_saved_drink():
    from app.orders import OrderRow
    from app.orders import till_summary
    from app.models import User

    no_drink = OrderRow(user=User(id="x", display_name="Nobody"), saved=None, line="-", is_self=False)
    a = _row("Alice", base_id="latte")
    assert till_summary([no_drink, a]) == ["1x latte"]


def test_summary_orders_groups_by_first_seen():
    from app.orders import till_summary

    rows = [
        _row("Alice", base_id="latte"),
        _row("Bob", base_id="espresso", size=None, milk=None),
        _row("Cat", base_id="latte"),
    ]
    assert till_summary(rows) == ["2x latte", "1x espresso"]
