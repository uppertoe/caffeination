"""Helpers for the in-progress group order of a single owner."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlmodel import Session, select

from app.drinks import format_drink, get_saved_drink
from app.menu import get_drink
from app.models import OrderItem, SavedDrink, User
from app.users import _as_naive_utc, can_edit_person, touch_last_active

# An open order goes stale this long after its FIRST item was added; the
# whole thing is cleared lazily on the next render. Coffee runs are a
# same-morning affair — yesterday's order shouldn't greet you today.
ORDER_TTL = timedelta(hours=12)

# Roster split: anyone without a sign of life in this window (visiting,
# saving a drink, or being picked for an order) drops into the collapsed
# "inactive" group — rotating registrars sink out of the picker after their
# rotation ends without anyone having to delete them.
ACTIVE_WINDOW = timedelta(days=90)


@dataclass
class OrderRow:
    user: User
    saved: Optional[SavedDrink]
    line: str
    is_self: bool
    can_edit: bool = False


def is_self_excluded(session: Session, owner_id: str) -> bool:
    """True when the owner has opted out of their own order.

    The owner is included implicitly (no membership row), so a self-targeting
    OrderItem is an OPT-OUT marker, not a membership row: it exists only while
    the owner has removed themselves ("buying for others, not me"). It shares
    the order's lifecycle — cleared by clear_order and expired by the TTL —
    so tomorrow's order includes the owner again by default.
    """
    return session.get(OrderItem, (owner_id, owner_id)) is not None


def _delete_self_opt_out(session: Session, owner_id: str) -> None:
    marker = session.get(OrderItem, (owner_id, owner_id))
    if marker is not None:
        session.delete(marker)
        session.commit()


def add_to_order(session: Session, owner_id: str, target_user_id: str) -> None:
    if target_user_id == owner_id:
        # Owner is included implicitly; "adding yourself" just clears any
        # opt-out marker (see is_self_excluded).
        _delete_self_opt_out(session, owner_id)
        return
    target = session.get(User, target_user_id)
    if target is None or target.display_name is None:
        return
    if target.one_off and target.created_by != owner_id:
        # One-offs belong to the order of whoever created them; letting a
        # second owner hold a reference would orphan it on deletion.
        return
    if session.get(OrderItem, (owner_id, target_user_id)) is not None:
        return
    session.add(OrderItem(owner_id=owner_id, target_user_id=target_user_id))
    session.commit()
    # Being picked for a coffee run is a sign of life — it keeps colleagues
    # who never open the app themselves in the roster's active group.
    touch_last_active(session, target)


def remove_from_order(session: Session, owner_id: str, target_user_id: str) -> None:
    if target_user_id == owner_id:
        # Removing yourself records the opt-out marker rather than deleting
        # anything — there is no membership row for the owner to delete.
        if not is_self_excluded(session, owner_id):
            session.add(OrderItem(owner_id=owner_id, target_user_id=owner_id))
            session.commit()
        return
    item = session.get(OrderItem, (owner_id, target_user_id))
    if item is not None:
        session.delete(item)
        session.commit()
    # A one-off only ever lives in its creator's order, so removing it should
    # delete the throwaway person + drink rather than orphan them.
    target = session.get(User, target_user_id)
    if target is not None and target.one_off and target.created_by == owner_id:
        sd = session.get(SavedDrink, target_user_id)
        if sd is not None:
            session.delete(sd)
        session.delete(target)
        session.commit()


def clear_order(session: Session, owner_id: str) -> None:
    """Empty the owner's open order, cleaning up one-off people with it.

    Also resets any self opt-out marker: a cleared order is back to the
    default state, which includes the owner.
    """
    _delete_self_opt_out(session, owner_id)
    items = session.exec(
        select(OrderItem).where(OrderItem.owner_id == owner_id)
    ).all()
    for item in items:
        remove_from_order(session, owner_id, item.target_user_id)


def purge_expired_order(
    session: Session, owner_id: str, now: Optional[datetime] = None
) -> None:
    items = session.exec(
        select(OrderItem).where(OrderItem.owner_id == owner_id)
    ).all()
    if not items:
        return
    now = now or datetime.now(timezone.utc)
    oldest = min(_as_naive_utc(item.added_at) for item in items)
    if _as_naive_utc(now) - oldest >= ORDER_TTL:
        clear_order(session, owner_id)


def _line_for(saved: Optional[SavedDrink]) -> str:
    if saved is None:
        return "(no drink saved yet)"
    drink = get_drink(saved.base_id)
    return format_drink(drink, saved) if drink else saved.base_id


def order_rows(session: Session, owner_id: str) -> list[OrderRow]:
    """Owner (if they have a drink) followed by each added user.

    Stale orders are purged here so every render (page load or HTMX
    fragment) sees at most a 12-hour-old order.
    """
    purge_expired_order(session, owner_id)
    rows: list[OrderRow] = []
    owner = session.get(User, owner_id)
    owner_drink = get_saved_drink(session, owner_id)
    if (
        owner is not None
        and owner_drink is not None
        and not is_self_excluded(session, owner_id)
    ):
        rows.append(OrderRow(owner, owner_drink, _line_for(owner_drink), True))

    items = session.exec(
        select(OrderItem).where(OrderItem.owner_id == owner_id)
    ).all()
    for item in items:
        if item.target_user_id == owner_id:
            continue  # the self opt-out marker is not an order line
        u = session.get(User, item.target_user_id)
        if u is None:
            continue
        sd = get_saved_drink(session, u.id)
        rows.append(
            OrderRow(u, sd, _line_for(sd), False, can_edit_person(owner_id, u))
        )
    return rows


def _is_active(user: User, now: datetime) -> bool:
    ref = user.last_active_at or user.created_at
    return _as_naive_utc(now) - _as_naive_utc(ref) < ACTIVE_WINDOW


def roster_candidates(
    session: Session, owner_id: str
) -> tuple[list[tuple[User, str]], list[tuple[User, str]]]:
    """Users (other than owner) who have a saved drink and aren't in the order.

    Returns (active, inactive): alphabetical within each group, split on
    ACTIVE_WINDOW. Bucketing rather than sorting by raw recency keeps the
    list stable day to day while stale names still sink as a group.
    """
    in_order = {
        i.target_user_id
        for i in session.exec(
            select(OrderItem).where(OrderItem.owner_id == owner_id)
        ).all()
    }
    excluded = in_order | {owner_id}

    users_by_id = {
        u.id: u
        for u in session.exec(select(User)).all()
        if u.display_name and not u.one_off
    }
    drinks_by_user = {
        sd.user_id: sd for sd in session.exec(select(SavedDrink)).all()
    }

    now = datetime.now(timezone.utc)
    active: list[tuple[User, str]] = []
    inactive: list[tuple[User, str]] = []
    for uid, user in users_by_id.items():
        if uid in excluded:
            continue
        sd = drinks_by_user.get(uid)
        if sd is None:
            continue
        (active if _is_active(user, now) else inactive).append((user, _line_for(sd)))
    active.sort(key=lambda pair: pair[0].display_name.lower())
    inactive.sort(key=lambda pair: pair[0].display_name.lower())
    return active, inactive


# ---------------------------------------------------------------------------
# Till summary — collapses identical drinks into "Nx <line>" entries.
# ---------------------------------------------------------------------------


def till_summary(rows: list[OrderRow]) -> list[str]:
    """Group order rows into till-ready lines.

    Drinks with free-text notes never merge — each is its own line. Drinks
    without notes group by every ordered option (base, size, milk,
    strength, temp, sweetener, length) per the coffee-taxonomy skill.
    """
    groups: "OrderedDict[tuple, dict]" = OrderedDict()
    standalone: list[str] = []

    for row in rows:
        sd = row.saved
        if sd is None:
            continue
        if sd.notes:
            standalone.append(row.line)
            continue
        key = (
            sd.base_id,
            sd.size,
            sd.milk,
            sd.strength,
            sd.temp,
            sd.sweetener,
            sd.length,
        )
        if key not in groups:
            groups[key] = {"count": 0, "line": row.line}
        groups[key]["count"] += 1

    lines = [f"{g['count']}x {g['line']}" for g in groups.values()]
    lines.extend(f"1x {line}" for line in standalone)
    return lines
