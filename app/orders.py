"""Helpers for the in-progress group order of a single owner."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import NamedTuple, Optional

from sqlalchemy import func
from sqlmodel import Session, delete, select

from app.drinks import LINE_FIELDS, format_line
from app.models import OrderItem, SavedDrink, User
from app.users import ROSTER_FILTER, _as_naive_utc, can_edit_person, touch_last_active

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


class RosterEntry(NamedTuple):
    """One pickable person in the "Add to your order" list. Plain columns
    rather than User/SavedDrink objects: the roster is the one part of the
    page that scales with the whole office, and hydrating two ORM objects
    per row was most of the render cost at a few hundred people."""

    id: str
    display_name: str
    line: str


@dataclass
class OrderView:
    """Everything the order section renders, loaded in three statements:
    the order (items joined to people and drinks), the owner's own drink,
    and the roster of everyone not yet in the order."""

    rows: list[OrderRow] = field(default_factory=list)
    self_excluded: bool = False
    owner_drink: Optional[SavedDrink] = None
    roster: list[RosterEntry] = field(default_factory=list)
    roster_inactive: list[RosterEntry] = field(default_factory=list)


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
    # A one-off only ever lives in its creator's order, so removing it should
    # delete the throwaway person + drink rather than orphan them.
    target = session.get(User, target_user_id)
    if target is not None and target.one_off and target.created_by == owner_id:
        session.exec(delete(SavedDrink).where(SavedDrink.user_id == target_user_id))
        session.delete(target)
    session.commit()


def _one_offs_of(owner_id: str):
    return select(User.id).where(User.one_off == True, User.created_by == owner_id)  # noqa: E712


def clear_order(session: Session, owner_id: str) -> None:
    """Empty the owner's open order, cleaning up one-off people with it.

    Also resets any self opt-out marker: a cleared order is back to the
    default state, which includes the owner. Three set-based deletes and one
    commit, however many people are in the order. One-offs can only ever sit
    in their creator's order (add_to_order refuses anyone else), so "every
    one-off this owner created" is exactly the set to remove.
    """
    session.exec(delete(SavedDrink).where(SavedDrink.user_id.in_(_one_offs_of(owner_id))))
    session.exec(delete(OrderItem).where(OrderItem.owner_id == owner_id))
    session.exec(delete(User).where(User.one_off == True, User.created_by == owner_id))  # noqa: E712
    session.commit()


def _expired(items: list[OrderItem], now: Optional[datetime] = None) -> bool:
    if not items:
        return False
    now = now or datetime.now(timezone.utc)
    oldest = min(_as_naive_utc(item.added_at) for item in items)
    return _as_naive_utc(now) - oldest >= ORDER_TTL


def purge_expired_order(
    session: Session, owner_id: str, now: Optional[datetime] = None
) -> bool:
    """Clear the owner's order if its first item is older than ORDER_TTL.
    Returns True if it was cleared."""
    items = session.exec(
        select(OrderItem).where(OrderItem.owner_id == owner_id)
    ).all()
    if not _expired(items, now):
        return False
    clear_order(session, owner_id)
    return True


def _line_for(saved: Optional[SavedDrink]) -> str:
    if saved is None:
        return "(no drink saved yet)"
    return format_line(*(getattr(saved, f) for f in LINE_FIELDS))


def load_order_view(session: Session, owner: User) -> OrderView:
    """Load the owner's order section in a fixed number of statements,
    independent of how many people are in the order or on the roster.

    Stale orders are purged here so every render (page load or HTMX
    fragment) sees at most a 12-hour-old order.
    """
    owner_id = owner.id

    # 1. The order: every item joined to its person and their drink. A
    #    person can vanish between the item being added and now (deleted
    #    themselves), so the joins are outer and such rows are skipped.
    order_q = (
        select(OrderItem, User, SavedDrink)
        .join(User, User.id == OrderItem.target_user_id, isouter=True)
        .join(SavedDrink, SavedDrink.user_id == OrderItem.target_user_id, isouter=True)
        .where(OrderItem.owner_id == owner_id)
        .order_by(OrderItem.added_at, OrderItem.target_user_id)
    )
    joined = session.exec(order_q).all()
    items = [item for item, _, _ in joined]
    if _expired(items):
        clear_order(session, owner_id)
        joined = []

    view = OrderView()
    view.self_excluded = any(item.target_user_id == owner_id for item, _, _ in joined)

    # 2. The owner's own drink (an identity-map hit when the drink card
    #    already loaded it this request).
    view.owner_drink = session.get(SavedDrink, owner_id)
    if view.owner_drink is not None and not view.self_excluded:
        view.rows.append(
            OrderRow(owner, view.owner_drink, _line_for(view.owner_drink), True)
        )
    for item, u, sd in joined:
        if item.target_user_id == owner_id or u is None:
            continue  # the self opt-out marker is not an order line
        view.rows.append(
            OrderRow(u, sd, _line_for(sd), False, can_edit_person(owner_id, u))
        )

    # 3. The roster: roster users with a saved drink, minus the owner and
    #    anyone already in the order. Filtered and ordered in SQL, and only
    #    the columns the list renders are fetched. The active/inactive split
    #    is a per-row date comparison done here so the naive-vs-aware
    #    datetime handling stays in one place.
    in_order = select(OrderItem.target_user_id).where(OrderItem.owner_id == owner_id)
    roster_q = (
        select(
            User.id,
            User.display_name,
            User.last_active_at,
            User.created_at,
            *(getattr(SavedDrink, f) for f in LINE_FIELDS),
        )
        .join(SavedDrink, SavedDrink.user_id == User.id)
        .where(*ROSTER_FILTER, User.id != owner_id, User.id.not_in(in_order))
        .order_by(func.lower(User.display_name))
    )
    cutoff = _as_naive_utc(datetime.now(timezone.utc)) - ACTIVE_WINDOW
    for uid, name, last_active, created, *drink in session.exec(roster_q).all():
        active = _as_naive_utc(last_active or created) >= cutoff
        bucket = view.roster if active else view.roster_inactive
        bucket.append(RosterEntry(uid, name, format_line(*drink)))
    return view


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
