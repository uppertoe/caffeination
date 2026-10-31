"""The round log: what each closed order contained and whose drinks they
were. See the Round / RoundDrink models for what is kept and why."""

from __future__ import annotations

from datetime import timezone
from typing import Optional

from sqlalchemy import insert
from sqlmodel import Session, select

from app.models import OrderItem, Round, RoundDrink, SavedDrink, User
from app.users import _as_naive_utc

DRINK_FIELDS = ("base_id", "temp", "size", "milk", "strength", "sweetener", "length", "shots", "own_cup")


def record_round(session: Session, owner_id: str) -> Optional[Round]:
    """Log the owner's open order as a round, ahead of clear_order deleting it.

    Only an order someone else was added to counts as a round: an owner on
    their own has not placed a group order. Adds to the session without
    committing, so the log and the clear land in clear_order's one commit.
    Two selects (the items with their people, then the drinks) and two
    inserts, however big the order.
    """
    items = session.exec(
        select(OrderItem.target_user_id, OrderItem.added_at, User.one_off)
        .join(User, User.id == OrderItem.target_user_id, isouter=True)
        .where(OrderItem.owner_id == owner_id)
    ).all()
    # The owner's own row is the self opt-out marker, not an order line.
    others = {uid: bool(one_off) for uid, _, one_off in items if uid != owner_id}
    if not others:
        return None
    wanted = set(others)
    if len(others) == len(items):
        wanted.add(owner_id)
    saved = session.exec(select(SavedDrink).where(SavedDrink.user_id.in_(wanted))).all()
    if not saved:
        return None

    drinks = [
        {
            **{f: getattr(sd, f) for f in DRINK_FIELDS},
            "has_notes": bool(sd.notes and sd.notes.strip()),
            "guest": others.get(sd.user_id, False),
            # A guest is deleted with the order, so their id would dangle.
            "user_id": None if others.get(sd.user_id, False) else sd.user_id,
        }
        for sd in saved
    ]
    last_added = max(_as_naive_utc(added_at) for _, added_at, _ in items)
    rnd = Round(
        placed_at=last_added.replace(tzinfo=timezone.utc),  # SQLModel refuses naive
        drink_count=len(drinks),
        owner_id=owner_id,
    )
    session.add(rnd)
    session.flush()
    # Core insert on the table: one executemany. The ORM bulk path splits the
    # rows into a statement per pattern of NULL columns.
    session.exec(insert(RoundDrink.__table__), params=[{**d, "round_id": rnd.id} for d in drinks])
    return rnd
