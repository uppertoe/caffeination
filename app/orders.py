"""Helpers for the in-progress group order of a single owner."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import NamedTuple, Optional

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, delete, select

from app.drinks import LINE_FIELDS, format_line, line_drink
from app.models import LIVE_STATUSES, Order, OrderItem, SavedDrink, User
from app.rounds import record_round
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
    saved: Optional[SavedDrink]  # the drink on the line, usual or one-off
    line: str
    is_self: bool
    can_edit: bool = False
    location: Optional[str] = None
    via_code: bool = False  # added by someone with the join code
    one_off_drink: bool = False  # a drink for this order only, not their usual
    joined_by: Optional[str] = None  # the joiner who added it by code


class RosterEntry(NamedTuple):
    """One pickable person in the "Add to your order" list. Plain columns
    rather than User/SavedDrink objects: the roster is the one part of the
    page that scales with the whole office, and hydrating two ORM objects
    per row was most of the render cost at a few hundred people."""

    id: str
    display_name: str
    line: str
    in_order: bool = False  # already in the owner's order: shown selected


@dataclass
class OrderView:
    """Everything the order section renders, loaded in three statements:
    the order (items joined to people and drinks), the owner's own drink,
    and the roster, with anyone already in the order marked."""

    rows: list[OrderRow] = field(default_factory=list)
    order: Optional[Order] = None  # None until the owner first changes it
    self_excluded: bool = False
    owner_drink: Optional[SavedDrink] = None
    roster: list[RosterEntry] = field(default_factory=list)
    roster_inactive: list[RosterEntry] = field(default_factory=list)
    # The owner's live advance orders, soonest closing first (loaded with
    # the everyday order only).
    advance: list["AdvanceSummary"] = field(default_factory=list)


@dataclass
class AdvanceSummary:
    order: Order
    drink_count: int


# Order statuses. Live orders (LIVE_STATUSES: open, ordered, on the way)
# have lines; the rest are how an order ended. A Ready order keeps its lines
# until it is pruned, so that joiners can still see their drink.
OPEN = "open"
ORDERED = "ordered"
ON_THE_WAY = "on_the_way"
READY = "ready"  # the buyer marked it ready, which completes it
CANCELLED = "cancelled"  # cleared by its owner
EXPIRED = "expired"  # closed by ORDER_TTL
EVERYDAY = "everyday"
ADVANCE = "advance"

# Closed Order rows are kept at least this long (so people who joined can be
# told how the order ended), then pruned by the statistics sweep.
CLOSED_ORDER_KEEP = timedelta(days=1)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _live_where(owner_id: str, order_id: Optional[int]) -> tuple:
    """Conditions for one of the owner's live orders: the advance order
    order_id or, when that is None, their everyday order."""
    which = Order.kind == EVERYDAY if order_id is None else Order.id == order_id
    return (Order.owner_id == owner_id, Order.status.in_(LIVE_STATUSES), which)


def live_order(session: Session, owner_id: str, order_id: Optional[int] = None) -> Optional[Order]:
    """The owner's live everyday order or, given order_id, that live order
    of theirs; None if there is no such order."""
    return session.exec(select(Order).where(*_live_where(owner_id, order_id))).first()


def open_everyday_order(session: Session, owner_id: str) -> Optional[Order]:
    """The owner's live everyday order, if they have one."""
    return live_order(session, owner_id)


def _live_order_id(owner_id: str, order_id: Optional[int] = None):
    """A subquery for the id of that live order."""
    return select(Order.id).where(*_live_where(owner_id, order_id))


def order_for_write(
    session: Session, owner_id: str, order_id: Optional[int] = None
) -> Optional[Order]:
    """The live order a change applies to. The everyday order is created
    lazily, on the first change that needs one; an advance order must
    already exist (None if it doesn't, or is no longer live)."""
    order = live_order(session, owner_id, order_id)
    if order is None and order_id is None:
        order = Order(owner_id=owner_id, kind=EVERYDAY)
        session.add(order)
        try:
            session.flush()
        except IntegrityError:
            # A request alongside this one made it first (two quick taps on
            # an empty order); the partial unique index refused a second.
            session.rollback()
            order = live_order(session, owner_id)
    return order


def everyday_order_for_write(session: Session, owner_id: str) -> Order:
    """The owner's live everyday order, created if they have none yet."""
    return order_for_write(session, owner_id)


def advance_orders(session: Session, owner_id: str) -> list[Order]:
    """The owner's live advance orders, soonest closing first."""
    return list(
        session.exec(
            select(Order)
            .where(
                Order.owner_id == owner_id,
                Order.kind == ADVANCE,
                Order.status.in_(LIVE_STATUSES),
            )
            .order_by(Order.joins_close_at, Order.id)
        ).all()
    )


def is_self_excluded(session: Session, owner_id: str, order_id: Optional[int] = None) -> bool:
    """True when the owner has opted out of their own order.

    The owner is included implicitly (no OrderItem), so opting out is a
    timestamp on the order rather than a missing row. It shares the order's
    lifecycle, ending when the order is cleared or expires, so tomorrow's
    order includes the owner again by default.
    """
    order = live_order(session, owner_id, order_id)
    return order is not None and order.owner_excluded_at is not None


def add_to_order(
    session: Session, owner_id: str, target_user_id: str, order_id: Optional[int] = None
) -> None:
    if target_user_id == owner_id:
        # Owner is included implicitly; "adding yourself" just ends any
        # opt-out (see is_self_excluded).
        order = live_order(session, owner_id, order_id)
        if order is not None and order.owner_excluded_at is not None:
            order.owner_excluded_at = None
            session.add(order)
            session.commit()
        return
    target = session.get(User, target_user_id)
    if target is None or target.display_name is None:
        return
    if target.one_off and target.created_by != owner_id:
        # One-offs belong to the order of whoever created them; letting a
        # second owner hold a reference would orphan it on deletion.
        return
    if target.one_off and session.exec(
        select(OrderItem.order_id).where(OrderItem.target_user_id == target_user_id)
    ).first() is not None:
        # Nor can a one-off be in two of the creator's orders: clearing one
        # would delete them from the other.
        return
    order = order_for_write(session, owner_id, order_id)
    if order is None:
        return
    if session.get(OrderItem, (order.id, target_user_id)) is not None:
        return
    session.add(OrderItem(order_id=order.id, target_user_id=target_user_id))
    try:
        session.commit()
    except IntegrityError:
        # The same Add, sent twice side by side: the first added the line.
        session.rollback()
        return
    # Being picked for a coffee run is a sign of life — it keeps colleagues
    # who never open the app themselves in the roster's active group.
    touch_last_active(session, target)


def remove_from_order(
    session: Session, owner_id: str, target_user_id: str, order_id: Optional[int] = None
) -> None:
    if target_user_id == owner_id:
        # Removing yourself records the opt-out rather than deleting
        # anything — there is no line for the owner to delete.
        order = order_for_write(session, owner_id, order_id)
        if order is None:
            return
        if order.owner_excluded_at is None:
            order.owner_excluded_at = _now()
            session.add(order)
        session.commit()
        return
    # One statement, without loading the order first.
    removed = session.exec(
        delete(OrderItem).where(
            OrderItem.target_user_id == target_user_id,
            OrderItem.order_id.in_(_live_order_id(owner_id, order_id)),
        )
    ).rowcount
    # A one-off only ever lives in one order (add_to_order sees to that), so
    # removing their line should delete the throwaway person and drink
    # rather than orphan them. Nothing is deleted if no line was: a stale
    # page must not take a guest out from under a finished order.
    target = session.get(User, target_user_id) if removed else None
    if target is not None and target.one_off and target.created_by == owner_id:
        session.exec(delete(SavedDrink).where(SavedDrink.user_id == target_user_id))
        session.delete(target)
    session.commit()


def delete_lines(session: Session, order_ids) -> None:
    """Delete the lines of the given orders (ids, or a subquery of them) and
    the one-off guests in them, without committing. Guests are found through
    the lines, since a guest belongs to exactly one order, so they go first."""
    in_orders = OrderItem.order_id.in_(order_ids)
    guests = (
        select(User.id)
        .join(OrderItem, OrderItem.target_user_id == User.id)
        .where(in_orders, User.one_off == True)  # noqa: E712
    )
    session.exec(delete(SavedDrink).where(SavedDrink.user_id.in_(guests)))
    session.exec(
        delete(User).where(
            User.one_off == True,  # noqa: E712
            User.id.in_(select(OrderItem.target_user_id).where(in_orders)),
        )
    )
    session.exec(delete(OrderItem).where(in_orders))


def close_order(
    session: Session, order: Order, status: str, *, record: bool = True
) -> None:
    """End a live order: log it as a round (unless record is False, as when
    the owner deletes themselves), delete its lines and the one-off guests
    in them, and keep the Order row with its closing status. A Ready order
    keeps its lines until it is pruned, for the joiners' pages.

    Set-based deletes and one commit, however many people are in the order.
    """
    if record:
        record_round(session, order)
    if status != READY:
        delete_lines(session, [order.id])
    order.status = status
    order.closed_at = _now()
    session.add(order)
    session.commit()


def clear_order(
    session: Session, owner_id: str, *, record: bool = True, order_id: Optional[int] = None
) -> None:
    """Empty the owner's everyday order (or the advance order order_id),
    cleaning up one-off people with it.

    A cleared order is closed; the next add starts a fresh one, which
    includes the owner by default. A cleared or expired order is the end of a
    round, so it is logged first (app.rounds) unless record is False, as when
    a user deletes themselves.
    """
    order = live_order(session, owner_id, order_id)
    if order is not None:
        close_order(session, order, CANCELLED, record=record)


def _first_change(order: Order, first_added: Optional[datetime]) -> Optional[datetime]:
    """When the order started ageing: its first line, or the owner opting
    out, whichever came first. None for an order with neither."""
    times = [t for t in (first_added, order.owner_excluded_at) if t is not None]
    return min((_as_naive_utc(t) for t in times), default=None)


def _expired(
    order: Order, first_added: Optional[datetime], now: Optional[datetime] = None
) -> bool:
    """ORDER_TTL after the order started ageing or, for a shared order, after
    its joins close, whichever is later. Counting from the closing time keeps
    an order collected the day before a meeting alive overnight."""
    started = _first_change(order, first_added)
    if order.status_at is not None:
        # A placed order lasts ORDER_TTL from its latest update at least.
        at = _as_naive_utc(order.status_at)
        started = at if started is None else max(started, at)
    if order.joins_close_at is not None:
        closes = _as_naive_utc(order.joins_close_at)
        started = closes if started is None else max(started, closes)
    if started is None:
        return False
    return _as_naive_utc(now or _now()) - started >= ORDER_TTL


def purge_expired_order(
    session: Session, owner_id: str, now: Optional[datetime] = None, order_id: Optional[int] = None
) -> bool:
    """Close the owner's order if it started more than ORDER_TTL ago.
    Returns True if it was closed."""
    order = live_order(session, owner_id, order_id)
    if order is None:
        return False
    first = session.exec(
        select(func.min(OrderItem.added_at)).where(OrderItem.order_id == order.id)
    ).one()
    if not _expired(order, first, now):
        return False
    close_order(session, order, EXPIRED)
    return True


def purge_all_expired_orders(session: Session, now: Optional[datetime] = None) -> int:
    """Close (and so log) every owner's expired order, not just the viewer's,
    and prune closed orders older than CLOSED_ORDER_KEEP.

    Expiry is otherwise lazy, on the owner's next render, so an order built,
    read off at the café and left alone would stay out of the statistics
    until its owner came back. Returns how many orders were closed."""
    now = now or _now()
    # Compared in Python, like the rest of the expiry code: SQLite reads
    # datetimes back naive, and SQLModel refuses to bind a naive one.
    firsts = session.exec(
        select(Order, func.min(OrderItem.added_at))
        .join(OrderItem, OrderItem.order_id == Order.id, isouter=True)
        .where(Order.status.in_(LIVE_STATUSES))
        .group_by(Order.id)
    ).all()
    expired = [order for order, first in firsts if _expired(order, first, now)]
    for order in expired:
        close_order(session, order, EXPIRED)
    if expired:
        # Pruning rides along only when something expired, which happens
        # most days, so the usual statistics render costs nothing extra.
        old = select(Order.id).where(
            Order.status.not_in(LIVE_STATUSES), Order.closed_at < now - CLOSED_ORDER_KEEP
        )
        delete_lines(session, old)  # only Ready orders still have any
        session.exec(delete(Order).where(Order.id.in_(old)))
        session.commit()
    return len(expired)


def _line_for(saved: Optional[SavedDrink]) -> str:
    if saved is None:
        return "(no drink saved yet)"
    return format_line(*(getattr(saved, f) for f in LINE_FIELDS))


def load_order_view(session: Session, owner: User, order_id: Optional[int] = None) -> OrderView:
    """Load the owner's order section in a fixed number of statements,
    independent of how many people are in the order or on the roster.

    Stale orders are purged here so every render (page load or HTMX
    fragment) sees at most a 12-hour-old order.
    """
    owner_id = owner.id

    # 1. The order: the owner's live everyday order (or the advance order
    #    order_id) with every line joined to its person and their drink, in
    #    one statement. For the everyday order the same statement also
    #    brings the owner's live advance orders, for the list below it. The
    #    line joins are outer so an order with no lines still comes back
    #    (its opt-out matters), and a person can vanish between being added
    #    and now (deleted themselves), so such rows are skipped below.
    where = (
        _live_where(owner_id, order_id)
        if order_id is not None
        else (Order.owner_id == owner_id, Order.status.in_(LIVE_STATUSES))
    )
    order_q = (
        select(Order, OrderItem, User, SavedDrink)
        .join(OrderItem, OrderItem.order_id == Order.id, isouter=True)
        .join(User, User.id == OrderItem.target_user_id, isouter=True)
        .join(SavedDrink, SavedDrink.user_id == OrderItem.target_user_id, isouter=True)
        .where(*where)
        .order_by(OrderItem.added_at, OrderItem.target_user_id)
    )
    by_order: dict[int, tuple[Order, list]] = {}
    for o, item, u, sd in session.exec(order_q).all():
        lines = by_order.setdefault(o.id, (o, []))[1]
        if item is not None:
            lines.append((item, u, sd))
    for o, lines in list(by_order.values()):
        first = min((item.added_at for item, _, _ in lines), default=None)
        if _expired(o, first):
            close_order(session, o, EXPIRED)
            del by_order[o.id]
    main = next(
        (
            (o, lines)
            for o, lines in by_order.values()
            if (o.id == order_id if order_id is not None else o.kind == EVERYDAY)
        ),
        (None, []),
    )
    order, joined = main

    view = OrderView(order=order)
    view.self_excluded = order is not None and order.owner_excluded_at is not None

    # 2. The owner's own drink (an identity-map hit when the drink card
    #    already loaded it this request).
    view.owner_drink = session.get(SavedDrink, owner_id)
    if order_id is None:
        view.advance = sorted(
            (
                AdvanceSummary(
                    o,
                    sum(1 for item, u, sd in lines if u is not None and line_drink(item, sd))
                    + (1 if o.owner_excluded_at is None and view.owner_drink else 0),
                )
                for o, lines in by_order.values()
                if o.kind == ADVANCE
            ),
            key=lambda a: (_as_naive_utc(a.order.joins_close_at) if a.order.joins_close_at else datetime.max, a.order.id),
        )
    if view.owner_drink is not None and not view.self_excluded:
        view.rows.append(
            OrderRow(owner, view.owner_drink, _line_for(view.owner_drink), True)
        )
    for item, u, sd in joined:
        if u is None:
            continue
        drink = line_drink(item, sd)
        view.rows.append(
            OrderRow(
                u,
                drink,
                _line_for(drink),
                False,
                can_edit_person(owner_id, u),
                location=item.location,
                via_code=item.joined_by is not None,
                joined_by=item.joined_by,
                one_off_drink=item.base_id is not None and not item.drink_fixed,
            )
        )

    # 3. The roster: roster users with a saved drink, minus the owner, with
    #    anyone already in the order marked rather than left out, so adding
    #    someone selects their row instead of removing it. Filtered and
    #    ordered in SQL, and only the columns the list renders are fetched.
    #    The active/inactive split is a per-row date comparison done here so
    #    the naive-vs-aware datetime handling stays in one place.
    in_order = {item.target_user_id for item, _, _ in joined}
    roster_q = (
        select(
            User.id,
            User.display_name,
            User.last_active_at,
            User.created_at,
            *(getattr(SavedDrink, f) for f in LINE_FIELDS),
        )
        .join(SavedDrink, SavedDrink.user_id == User.id)
        .where(*ROSTER_FILTER, User.id != owner_id)
        .order_by(func.lower(User.display_name))
    )
    cutoff = _as_naive_utc(datetime.now(timezone.utc)) - ACTIVE_WINDOW
    for uid, name, last_active, created, *drink in session.exec(roster_q).all():
        active = _as_naive_utc(last_active or created) >= cutoff
        bucket = view.roster if active else view.roster_inactive
        bucket.append(RosterEntry(uid, name, format_line(*drink), uid in in_order))
    return view


# ---------------------------------------------------------------------------
# Till summary — collapses identical drinks into "Nx <line>" entries.
# ---------------------------------------------------------------------------


def till_summary(rows: list[OrderRow]) -> list[str]:
    """Group order rows into till-ready lines.

    Drinks with free-text notes never merge — each is its own line. Drinks
    without notes group by every ordered option (base, size, milk,
    strength, temp, sweetener, length, shots, own cup) per the
    coffee-taxonomy skill.
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
            sd.shots,
            sd.own_cup,
        )
        if key not in groups:
            groups[key] = {"count": 0, "line": row.line}
        groups[key]["count"] += 1

    lines = [f"{g['count']}x {g['line']}" for g in groups.values()]
    lines.extend(f"1x {line}" for line in standalone)
    return lines
