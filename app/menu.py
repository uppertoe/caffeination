"""Drink taxonomy.

The constraint matrix lives here, not in templates or routes. See
`.claude/skills/coffee-taxonomy/SKILL.md` for the design rationale.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
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


class Sweetener(str, Enum):
    NONE = "none"
    ONE_SUGAR = "one_sugar"
    TWO_SUGAR = "two_sugar"
    HONEY = "honey"


class Temp(str, Enum):
    HOT = "hot"
    EXTRA_HOT = "extra_hot"
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
    Strength.REGULAR.value: "Regular strength",
    Strength.WEAK.value: "Weak",
    Strength.EXTRA_SHOT.value: "Extra shot",
    Strength.DECAF.value: "Decaf",
}

SWEETENER_LABELS: dict[str, str] = {
    Sweetener.NONE.value: "No sugar",
    Sweetener.ONE_SUGAR.value: "1 sugar",
    Sweetener.TWO_SUGAR.value: "2 sugars",
    Sweetener.HONEY.value: "Honey",
}

TEMP_LABELS: dict[str, str] = {
    Temp.HOT.value: "Hot",
    Temp.EXTRA_HOT.value: "Extra hot",
    Temp.ICED.value: "Iced",
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
    has_length: bool
    allows_strength: bool
    allows_iced: bool
    default_size: Optional[Size] = None
    # Shot counts the customer can choose between; () means not adjustable.
    # The first is the default.
    shot_choices: tuple[int, ...] = ()
    # Friendly note shown under the form when an axis is hidden by rule
    served_note: Optional[str] = None

    @property
    def allows_extra_hot(self) -> bool:
        """Extra hot is a request about steaming the milk, so it applies
        exactly to the drinks made with milk."""
        return self.milk_policy == MilkPolicy.REQUIRED

    def rules_dict(self) -> dict:
        """Serialised flags for the Alpine-driven form."""
        return {
            "sized": self.sized,
            "milkPolicy": self.milk_policy.value,
            "hasLength": self.has_length,
            "allowsStrength": self.allows_strength,
            "allowsIced": self.allows_iced,
            "allowsExtraHot": self.allows_extra_hot,
            "shotChoices": list(self.shot_choices),
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
        has_length=False,
        allows_strength=True,
        allows_iced=True,
        default_size=Size.SMALL,
    ),
    Drink(
        id="flat_white",
        display="Flat white",
        family=Family.MILK,
        milk_policy=MilkPolicy.REQUIRED,
        sized=True,
        has_length=False,
        allows_strength=True,
        allows_iced=False,
        default_size=Size.SMALL,
    ),
    Drink(
        id="cappuccino",
        display="Cappuccino",
        family=Family.MILK,
        milk_policy=MilkPolicy.REQUIRED,
        sized=True,
        has_length=False,
        allows_strength=True,
        allows_iced=False,
        default_size=Size.SMALL,
    ),
    Drink(
        id="magic",
        display="Magic",
        family=Family.MILK,
        milk_policy=MilkPolicy.REQUIRED,
        sized=False,
        has_length=False,
        allows_strength=False,
        allows_iced=False,
        served_note="Served as a 150 ml double ristretto + steamed milk. Fixed size.",
    ),
    Drink(
        id="piccolo",
        display="Piccolo",
        family=Family.ESPRESSO,
        milk_policy=MilkPolicy.REQUIRED,
        sized=False,
        has_length=False,
        allows_strength=False,
        allows_iced=False,
        served_note="A double ristretto with steamed milk in a 90 ml glass.",
    ),
    Drink(
        id="espresso",
        display="Espresso",
        family=Family.ESPRESSO,
        milk_policy=MilkPolicy.NONE,
        sized=False,
        has_length=False,
        allows_strength=False,
        allows_iced=False,
        shot_choices=(1, 2),
        served_note="Served black, no size.",
    ),
    Drink(
        id="macchiato",
        display="Macchiato",
        family=Family.ESPRESSO,
        milk_policy=MilkPolicy.DASH,
        sized=False,
        has_length=True,
        allows_strength=False,
        allows_iced=False,
        served_note="Long has more milk than short.",
    ),
    Drink(
        id="long_black",
        display="Long black",
        family=Family.BLACK,
        milk_policy=MilkPolicy.NONE,
        sized=True,
        has_length=False,
        allows_strength=True,
        allows_iced=True,
        default_size=Size.SMALL,
        served_note="Served black.",
    ),
    Drink(
        id="filter",
        display="Filter",
        family=Family.BLACK,
        milk_policy=MilkPolicy.NONE,
        sized=True,
        has_length=False,
        allows_strength=False,
        allows_iced=False,
        default_size=Size.SMALL,
        served_note="Batch-brewed filter coffee, served black.",
    ),
    Drink(
        id="cold_brew",
        display="Cold brew",
        family=Family.BLACK,
        milk_policy=MilkPolicy.NONE,
        sized=False,
        has_length=False,
        allows_strength=False,
        allows_iced=False,
        served_note="Served cold and black.",
    ),
    Drink(
        id="mocha",
        display="Mocha",
        family=Family.MILK_CHOC,
        milk_policy=MilkPolicy.REQUIRED,
        sized=True,
        has_length=False,
        allows_strength=True,
        allows_iced=True,
        default_size=Size.SMALL,
    ),
    Drink(
        id="hot_chocolate",
        display="Hot chocolate",
        family=Family.CHOC,
        milk_policy=MilkPolicy.REQUIRED,
        sized=True,
        has_length=False,
        allows_strength=False,
        allows_iced=True,
        default_size=Size.SMALL,
    ),
    Drink(
        id="chai_latte",
        display="Chai latte",
        family=Family.SPICE,
        milk_policy=MilkPolicy.REQUIRED,
        sized=True,
        has_length=False,
        allows_strength=False,
        allows_iced=True,
        default_size=Size.SMALL,
    ),
    Drink(
        id="dirty_chai",
        display="Dirty chai",
        family=Family.SPICE_ESP,
        milk_policy=MilkPolicy.REQUIRED,
        sized=True,
        has_length=False,
        allows_strength=True,
        allows_iced=True,
        default_size=Size.SMALL,
    ),
    Drink(
        id="matcha_latte",
        display="Matcha latte",
        family=Family.TEA,
        milk_policy=MilkPolicy.REQUIRED,
        sized=True,
        has_length=False,
        allows_strength=False,
        allows_iced=True,
        default_size=Size.SMALL,
    ),
)

DRINKS_BY_ID: dict[str, Drink] = {d.id: d for d in DRINKS}


def get_drink(base_id: str) -> Optional[Drink]:
    return DRINKS_BY_ID.get(base_id)


@lru_cache(maxsize=1)
def rules_for_template() -> dict[str, dict]:
    """The constraint matrix as the drink forms embed it (via |tojson). The
    menu is a code constant, so build it once; every dashboard render
    serialises it for the new-person slot."""
    return {d.id: d.rules_dict() for d in DRINKS}


# ---------------------------------------------------------------------------
# Caffeine estimates for the statistics page. Café coffee varies severalfold,
# so these are averages, not measurements of any one cup.
#
# Espresso: 107 mg per café serve, the Australian average measured by
# Desbrow et al. (Food Chem Toxicol 2007; J Food Compos Anal 2012), ranging
# from 25 to 214 mg. Filter, tea and hot chocolate: the Australian Institute
# of Sport's caffeine table. Cold brew, chai and matcha: no Australian
# measurements found, so these are overseas estimates.
# ---------------------------------------------------------------------------
MG_PER_SHOT = 107
MG_PER_SHOT_RANGE = (25, 214)

# Shots in each espresso drink; milk drinks scale with size.
_FIXED_SHOTS: dict[str, float] = {
    "macchiato": 1,
    "piccolo": 1,
    "magic": 2,
}
_SHOTS_BY_SIZE: dict[str, float] = {"small": 1, "regular": 1, "large": 2}
# Drinks without espresso, by their own caffeine.
_NON_ESPRESSO_MG: dict[str, float] = {
    "hot_chocolate": 5,
    "chai_latte": 35,
    "matcha_latte": 70,
    "filter": 80,
    "cold_brew": 150,
}


def estimated_caffeine_mg(
    base_id: str, size: Optional[str], strength: str, shots: Optional[int] = None
) -> float:
    """Estimated caffeine in one drink, in milligrams."""
    if base_id in _NON_ESPRESSO_MG:
        return _NON_ESPRESSO_MG[base_id]
    if base_id == "espresso":
        n = shots or 1
    elif base_id in _FIXED_SHOTS:
        n = _FIXED_SHOTS[base_id]
    elif base_id == "long_black":
        n = 2 if size != "large" else 3
    else:
        n = _SHOTS_BY_SIZE.get(size or "regular", 1)
    if strength == Strength.EXTRA_SHOT.value:
        n += 1
    elif strength == Strength.WEAK.value:
        n = max(n - 0.5, 0.5)
    mg = n * MG_PER_SHOT
    if strength == Strength.DECAF.value:
        mg *= 0.03
    if base_id == "dirty_chai":
        mg += _NON_ESPRESSO_MG["chai_latte"]
    return mg
