"""The round log: what each closed order contained and whose drinks they
were. See the Round / RoundDrink models for what is kept and why."""

from __future__ import annotations

from datetime import timezone
from typing import Optional

from sqlalchemy import insert
from sqlmodel import Session, select

from app.drinks import line_drink
from app.models import Order, OrderItem, Round, RoundDrink, SavedDrink, User
from app.users import _as_naive_utc

DRINK_FIELDS = ("base_id", "temp", "size", "milk", "strength", "sweetener", "length", "shots", "own_cup")


def record_round(session: Session, order: Order) -> Optional[Round]:
    """Log an open order as a round, ahead of its lines being deleted.

    Only an order someone else was added to counts as a round: an owner on
    their own has not placed a group order. Adds to the session without
    committing, so the log and the close land in the caller's one commit.
    Two selects (the lines with their people and drinks, then the owner's
    drink) and two inserts, however big the order. A line's one-off drink
    is logged in place of the person's usual.
    """
    owner_id = order.owner_id
    lines = session.exec(
        select(OrderItem, User.one_off, SavedDrink)
        .join(User, User.id == OrderItem.target_user_id, isouter=True)
        .join(SavedDrink, SavedDrink.user_id == OrderItem.target_user_id, isouter=True)
        .where(OrderItem.order_id == order.id)
    ).all()
    lines = [(item, bool(one_off), sd) for item, one_off, sd in lines if item.target_user_id != owner_id]
    if not lines:
        return None
    entries = [(item.target_user_id, guest, line_drink(item, sd)) for item, guest, sd in lines]
    if order.owner_excluded_at is None:
        own = session.get(SavedDrink, owner_id)
        entries.append((owner_id, False, own))
    entries = [(uid, guest, sd) for uid, guest, sd in entries if sd is not None]
    if not entries:
        return None

    drinks = [
        {
            **{f: getattr(sd, f) for f in DRINK_FIELDS},
            "has_notes": bool(sd.notes and sd.notes.strip()),
            "guest": guest,
            # A guest is deleted with the order, so their id would dangle.
            "user_id": None if guest else uid,
        }
        for uid, guest, sd in entries
    ]
    # Stamped with the last change to who is in it, which includes the owner
    # taking themselves out.
    times = [item.added_at for item, _, _ in lines]
    if order.owner_excluded_at is not None:
        times.append(order.owner_excluded_at)
    last_added = max(_as_naive_utc(t) for t in times)
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
