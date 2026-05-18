"""Drink taxonomy.

The constraint matrix lives here, not in templates or routes. See
`.claude/skills/coffee-taxonomy/SKILL.md` for the design rationale.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class Family(str, Enum):
    ESPRESSO = "espresso"
    BLACK = "black"
    MILK = "milk"
    MILK_CHOC = "milk_choc"
    CHOC = "choc"
    SPICE = "spice"
    SPICE_ESP = "spice_esp"
    TEA = "tea"


class MilkPolicy(str, Enum):
    NONE = "none"        # served black; hide milk + strength
    DASH = "dash"        # a dash of milk; hide milk + strength
    REQUIRED = "required"  # milk is part of the drink


class Size(str, Enum):
    SMALL = "small"
    REGULAR = "regular"
    LARGE = "large"


class Milk(str, Enum):
    FULL_CREAM = "full_cream"
    SKINNY = "skinny"
    OAT = "oat"
    SOY = "soy"
    ALMOND = "almond"
    LACTOSE_FREE = "lactose_free"


class Strength(str, Enum):
    REGULAR = "regular"
    WEAK = "weak"
    EXTRA_SHOT = "extra_shot"
    DECAF = "decaf"
    HALF_CAF = "half_caf"


class Sweetener(str, Enum):
    NONE = "none"
    ONE_SUGAR = "one_sugar"
    TWO_SUGAR = "two_sugar"
    HONEY = "honey"


class Temp(str, Enum):
    HOT = "hot"
    ICED = "iced"


class Length(str, Enum):
    SHORT = "short"
    LONG = "long"


MILK_LABELS: dict[str, str] = {
    Milk.FULL_CREAM.value: "Full cream",
    Milk.SKINNY.value: "Skinny",
    Milk.OAT.value: "Oat",
    Milk.SOY.value: "Soy",
    Milk.ALMOND.value: "Almond",
    Milk.LACTOSE_FREE.value: "Lactose-free",
}

STRENGTH_LABELS: dict[str, str] = {
    Strength.REGULAR.value: "Regular",
    Strength.WEAK.value: "Weak",
    Strength.EXTRA_SHOT.value: "Extra shot",
    Strength.DECAF.value: "Decaf",
    Strength.HALF_CAF.value: "Half-caf",
}

SWEETENER_LABELS: dict[str, str] = {
    Sweetener.NONE.value: "No sugar",
    Sweetener.ONE_SUGAR.value: "1 sugar",
    Sweetener.TWO_SUGAR.value: "2 sugars",
    Sweetener.HONEY.value: "Honey",
}

SIZE_LABELS: dict[str, str] = {
    Size.SMALL.value: "Small",
    Size.REGULAR.value: "Regular",
    Size.LARGE.value: "Large",
}


@dataclass(frozen=True)
class Drink:
    id: str
    display: str
    family: Family
    milk_policy: MilkPolicy
    sized: bool
    shot_choices: tuple[int, ...]
    has_length: bool
    allows_strength: bool
    allows_iced: bool
    default_shots: int
    default_size: Optional[Size] = None
    # Friendly note shown under the form when an axis is hidden by rule
    served_note: Optional[str] = None

    def rules_dict(self) -> dict:
        """Serialised flags for the Alpine-driven form."""
        return {
            "sized": self.sized,
            "milkPolicy": self.milk_policy.value,
            "shotChoices": list(self.shot_choices),
            "hasLength": self.has_length,
            "allowsStrength": self.allows_strength,
            "allowsIced": self.allows_iced,
            "defaultShots": self.default_shots,
            "defaultSize": self.default_size.value if self.default_size else None,
            "servedNote": self.served_note,
        }


# ---------------------------------------------------------------------------
# The menu. Ordering here is the order shown in the form.
# ---------------------------------------------------------------------------
DRINKS: tuple[Drink, ...] = (
    Drink(
        id="latte",
        display="Latte",
        family=Family.MILK,
        milk_policy=MilkPolicy.REQUIRED,
        sized=True,
        shot_choices=(1, 2, 3),
        has_length=False,
        allows_strength=True,
        allows_iced=True,
        default_shots=1,
        default_size=Size.REGULAR,
    ),
    Drink(
        id="flat_white",
        display="Flat white",
        family=Family.MILK,
        milk_policy=MilkPolicy.REQUIRED,
        sized=True,
        shot_choices=(1, 2, 3),
        has_length=False,
        allows_strength=True,
        allows_iced=False,
        default_shots=1,
        default_size=Size.REGULAR,
    ),
    Drink(
        id="cappuccino",
        display="Cappuccino",
        family=Family.MILK,
        milk_policy=MilkPolicy.REQUIRED,
        sized=True,
        shot_choices=(1, 2, 3),
        has_length=False,
        allows_strength=True,
        allows_iced=False,
        default_shots=1,
        default_size=Size.REGULAR,
    ),
    Drink(
        id="magic",
        display="Magic",
        family=Family.MILK,
        milk_policy=MilkPolicy.REQUIRED,
        sized=False,
        shot_choices=(),
        has_length=False,
        allows_strength=False,
        allows_iced=False,
        default_shots=2,
        served_note="Served as a 150 ml double ristretto + steamed milk. Fixed size.",
    ),
    Drink(
        id="piccolo",
        display="Piccolo",
        family=Family.ESPRESSO,
        milk_policy=MilkPolicy.REQUIRED,
        sized=False,
        shot_choices=(),
        has_length=False,
        allows_strength=False,
        allows_iced=False,
        default_shots=2,
        served_note="A double ristretto with steamed milk in a 90 ml glass.",
    ),
    Drink(
        id="espresso",
        display="Espresso",
        family=Family.ESPRESSO,
        milk_policy=MilkPolicy.NONE,
        sized=False,
        shot_choices=(1, 2),
        has_length=False,
        allows_strength=False,
        allows_iced=False,
        default_shots=1,
        served_note="Served black, no size.",
    ),
    Drink(
        id="macchiato",
        display="Macchiato",
        family=Family.ESPRESSO,
        milk_policy=MilkPolicy.DASH,
        sized=False,
        shot_choices=(),
        has_length=True,
        allows_strength=False,
        allows_iced=False,
        default_shots=1,
        served_note="Long has more milk than short; both are single-shot.",
    ),
    Drink(
        id="long_black",
        display="Long black",
        family=Family.BLACK,
        milk_policy=MilkPolicy.NONE,
        sized=True,
        shot_choices=(1, 2),
        has_length=False,
        allows_strength=True,
        allows_iced=True,
        default_shots=2,
        default_size=Size.REGULAR,
        served_note="Served black.",
    ),
    Drink(
        id="mocha",
        display="Mocha",
        family=Family.MILK_CHOC,
        milk_policy=MilkPolicy.REQUIRED,
        sized=True,
        shot_choices=(1, 2),
        has_length=False,
        allows_strength=True,
        allows_iced=True,
        default_shots=1,
        default_size=Size.REGULAR,
    ),
    Drink(
        id="hot_chocolate",
        display="Hot chocolate",
        family=Family.CHOC,
        milk_policy=MilkPolicy.REQUIRED,
        sized=True,
        shot_choices=(),
        has_length=False,
        allows_strength=False,
        allows_iced=True,
        default_shots=0,
        default_size=Size.REGULAR,
    ),
    Drink(
        id="chai_latte",
        display="Chai latte",
        family=Family.SPICE,
        milk_policy=MilkPolicy.REQUIRED,
        sized=True,
        shot_choices=(),
        has_length=False,
        allows_strength=False,
        allows_iced=True,
        default_shots=0,
        default_size=Size.REGULAR,
    ),
    Drink(
        id="dirty_chai",
        display="Dirty chai",
        family=Family.SPICE_ESP,
        milk_policy=MilkPolicy.REQUIRED,
        sized=True,
        shot_choices=(1, 2),
        has_length=False,
        allows_strength=True,
        allows_iced=True,
        default_shots=1,
        default_size=Size.REGULAR,
    ),
    Drink(
        id="matcha_latte",
        display="Matcha latte",
        family=Family.TEA,
        milk_policy=MilkPolicy.REQUIRED,
        sized=True,
        shot_choices=(),
        has_length=False,
        allows_strength=False,
        allows_iced=True,
        default_shots=0,
        default_size=Size.REGULAR,
    ),
)

DRINKS_BY_ID: dict[str, Drink] = {d.id: d for d in DRINKS}


def get_drink(base_id: str) -> Optional[Drink]:
    return DRINKS_BY_ID.get(base_id)


def rules_for_template() -> dict[str, dict]:
    return {d.id: d.rules_dict() for d in DRINKS}
