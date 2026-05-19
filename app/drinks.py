"""Persistence + normalisation + till-line formatting for saved drinks."""

from __future__ import annotations

from typing import Optional

from sqlmodel import Session

from app.menu import (
    MILK_LABELS,
    STRENGTH_LABELS,
    SWEETENER_LABELS,
    Drink,
    Length,
    Milk,
    MilkPolicy,
    Size,
    Strength,
    Sweetener,
    Temp,
    get_drink,
)
from app.models import SavedDrink, utcnow


def _enum_values(enum_cls) -> set[str]:
    return {m.value for m in enum_cls}


VALID_TEMPS = _enum_values(Temp)
VALID_SIZES = _enum_values(Size)
VALID_MILKS = _enum_values(Milk)
VALID_STRENGTHS = _enum_values(Strength)
VALID_SWEETENERS = _enum_values(Sweetener)
VALID_LENGTHS = _enum_values(Length)


def normalize(base_id: str, form: dict) -> Optional[dict]:
    """Coerce raw form fields into a stored shape, honouring per-drink rules.

    Returns None if the base_id is unknown.
    """
    drink = get_drink(base_id)
    if drink is None:
        return None

    def _pick(key: str, valid: set[str], fallback: str) -> str:
        v = form.get(key)
        return v if v in valid else fallback

    out: dict = {"base_id": drink.id}
    out["temp"] = (
        _pick("temp", VALID_TEMPS, Temp.HOT.value)
        if drink.allows_iced
        else Temp.HOT.value
    )
    out["size"] = (
        _pick(
            "size",
            VALID_SIZES,
            drink.default_size.value if drink.default_size else Size.REGULAR.value,
        )
        if drink.sized
        else None
    )
    out["milk"] = (
        _pick("milk", VALID_MILKS, Milk.FULL_CREAM.value)
        if drink.milk_policy == MilkPolicy.REQUIRED
        else None
    )
    if drink.shot_choices:
        try:
            n = int(form.get("shots", drink.default_shots))
        except (TypeError, ValueError):
            n = drink.default_shots
        out["shots"] = n if n in drink.shot_choices else drink.default_shots
    else:
        out["shots"] = drink.default_shots
    out["strength"] = (
        _pick("strength", VALID_STRENGTHS, Strength.REGULAR.value)
        if drink.allows_strength
        else Strength.REGULAR.value
    )
    out["sweetener"] = _pick("sweetener", VALID_SWEETENERS, Sweetener.NONE.value)
    out["length"] = (
        _pick("length", VALID_LENGTHS, Length.SHORT.value)
        if drink.has_length
        else None
    )
    notes = (form.get("notes") or "").strip()
    out["notes"] = notes[:80] or None
    return out


def upsert_saved_drink(session: Session, user_id: str, fields: dict) -> SavedDrink:
    existing = session.get(SavedDrink, user_id)
    if existing is None:
        sd = SavedDrink(user_id=user_id, **fields)
        session.add(sd)
    else:
        for k, v in fields.items():
            setattr(existing, k, v)
        existing.updated_at = utcnow()
        sd = existing
    session.commit()
    session.refresh(sd)
    return sd


def get_saved_drink(session: Session, user_id: str) -> Optional[SavedDrink]:
    return session.get(SavedDrink, user_id)


# ---------------------------------------------------------------------------
# Till-summary line formatting
# ---------------------------------------------------------------------------

# Espresso family: 2 shots reads as "double" rather than "2-shot espresso".
_SHOT_WORDS = {1: "single", 2: "double", 3: "triple"}


def format_drink(drink: Drink, sd: SavedDrink) -> str:
    """Render one till-summary line — lowercase, ordered like a barista call."""
    parts: list[str] = []

    # Shots-as-prefix for espresso family. Only mention shots when non-default.
    if drink.id == "espresso":
        parts.append(_SHOT_WORDS.get(sd.shots, f"{sd.shots}-shot"))
    elif drink.shot_choices and sd.shots != drink.default_shots:
        parts.append(f"{sd.shots}-shot")

    # Macchiato length comes before the drink name: "short macchiato".
    if drink.has_length and sd.length:
        parts.append(sd.length)

    # Size, only if not the default.
    if drink.sized and sd.size and (
        not drink.default_size or sd.size != drink.default_size.value
    ):
        parts.append(sd.size)

    if drink.allows_iced and sd.temp == Temp.ICED.value:
        parts.append("iced")

    if drink.allows_strength and sd.strength != Strength.REGULAR.value:
        parts.append(STRENGTH_LABELS[sd.strength].lower())

    if (
        drink.milk_policy == MilkPolicy.REQUIRED
        and sd.milk
        and sd.milk != Milk.FULL_CREAM.value
    ):
        parts.append(MILK_LABELS[sd.milk].lower())

    parts.append(drink.display.lower())
    line = " ".join(parts)

    extras: list[str] = []
    if sd.sweetener and sd.sweetener != Sweetener.NONE.value:
        extras.append(SWEETENER_LABELS[sd.sweetener].lower())
    if sd.notes:
        extras.append(sd.notes)
    if extras:
        line += " (" + ", ".join(extras) + ")"
    return line
