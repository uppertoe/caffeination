"""Helpers for the in-progress group order of a single owner."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sqlmodel import Session, select

from app.drinks import format_drink, get_saved_drink
from app.menu import get_drink
from app.models import OrderItem, SavedDrink, User


@dataclass
class OrderRow:
    user: User
    saved: Optional[SavedDrink]
    line: str
    is_self: bool


def add_to_order(session: Session, owner_id: str, target_user_id: str) -> None:
    if target_user_id == owner_id:
        return  # owner is included implicitly in the till summary
    target = session.get(User, target_user_id)
    if target is None or target.display_name is None:
        return
    if session.get(OrderItem, (owner_id, target_user_id)) is not None:
        return
    session.add(OrderItem(owner_id=owner_id, target_user_id=target_user_id))
    session.commit()


def remove_from_order(session: Session, owner_id: str, target_user_id: str) -> None:
    item = session.get(OrderItem, (owner_id, target_user_id))
    if item is not None:
        session.delete(item)
        session.commit()


def _line_for(saved: Optional[SavedDrink]) -> str:
    if saved is None:
        return "(no drink saved yet)"
    drink = get_drink(saved.base_id)
    return format_drink(drink, saved) if drink else saved.base_id


def order_rows(session: Session, owner_id: str) -> list[OrderRow]:
    """Owner (if they have a drink) followed by each added user."""
    rows: list[OrderRow] = []
    owner = session.get(User, owner_id)
    owner_drink = get_saved_drink(session, owner_id)
    if owner is not None and owner_drink is not None:
        rows.append(OrderRow(owner, owner_drink, _line_for(owner_drink), True))

    items = session.exec(
        select(OrderItem).where(OrderItem.owner_id == owner_id)
    ).all()
    for item in items:
        u = session.get(User, item.target_user_id)
        if u is None:
            continue
        sd = get_saved_drink(session, u.id)
        rows.append(OrderRow(u, sd, _line_for(sd), False))
    return rows


def roster_candidates(session: Session, owner_id: str) -> list[tuple[User, str]]:
    """Users (other than owner) who have a saved drink and aren't in the order."""
    in_order = {
        i.target_user_id
        for i in session.exec(
            select(OrderItem).where(OrderItem.owner_id == owner_id)
        ).all()
    }
    excluded = in_order | {owner_id}

    users_by_id = {u.id: u for u in session.exec(select(User)).all() if u.display_name}
    drinks_by_user = {
        sd.user_id: sd for sd in session.exec(select(SavedDrink)).all()
    }

    out: list[tuple[User, str]] = []
    for uid, user in users_by_id.items():
        if uid in excluded:
            continue
        sd = drinks_by_user.get(uid)
        if sd is None:
            continue
        out.append((user, _line_for(sd)))
    out.sort(key=lambda pair: pair[0].display_name.lower())
    return out
