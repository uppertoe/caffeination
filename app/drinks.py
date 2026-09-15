"""Persistence + normalisation + till-line formatting for saved drinks."""

from __future__ import annotations

from functools import lru_cache
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


# Field order shared by format_line and the column-only roster queries in
# app.orders / app.users, so a row can be formatted without building a
# SavedDrink object. Keep in sync with the SavedDrink model.
LINE_FIELDS = ("base_id", "length", "size", "temp", "strength", "milk", "sweetener", "notes")

_ICED = Temp.ICED.value
_REGULAR = Strength.REGULAR.value
_FULL_CREAM = Milk.FULL_CREAM.value
_NO_SWEETENER = Sweetener.NONE.value


def format_drink(drink: Drink, sd: SavedDrink) -> str:
    """Render one till-summary line for a saved drink."""
    return format_line(
        drink.id, sd.length, sd.size, sd.temp, sd.strength, sd.milk, sd.sweetener, sd.notes
    )


@lru_cache(maxsize=2048)
def format_line(
    base_id: str,
    length: Optional[str],
    size: Optional[str],
    temp: str,
    strength: str,
    milk: Optional[str],
    sweetener: str,
    notes: Optional[str],
) -> str:
    """Render one till-summary line — lowercase, ordered like a barista call.

    Memoised on the field tuple: an office has far fewer distinct drinks than
    people, and a full-roster render formats every one of them. An unknown
    base_id (a drink retired from the menu) renders as the raw id.
    """
    drink = get_drink(base_id)
    if drink is None:
        return base_id

    parts: list[str] = []

    # Macchiato length comes before the drink name: "short macchiato".
    if drink.has_length and length:
        parts.append(length)

    # Size, only if not the default.
    if drink.sized and size and (
        not drink.default_size or size != drink.default_size.value
    ):
        parts.append(size)

    if drink.allows_iced and temp == _ICED:
        parts.append("iced")

    if drink.allows_strength and strength != _REGULAR:
        parts.append(STRENGTH_LABELS[strength].lower())

    if drink.milk_policy == MilkPolicy.REQUIRED and milk and milk != _FULL_CREAM:
        parts.append(MILK_LABELS[milk].lower())

    parts.append(drink.display.lower())
    line = " ".join(parts)

    extras: list[str] = []
    if sweetener and sweetener != _NO_SWEETENER:
        extras.append(SWEETENER_LABELS[sweetener].lower())
    if notes:
        # Notes are free text; lowercase at display time so the till line
        # stays uniformly lowercase (the stored note keeps the user's typing).
        extras.append(notes.lower())
    if extras:
        line += " (" + ", ".join(extras) + ")"
    return line
