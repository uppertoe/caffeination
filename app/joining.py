"""Joining an order with its code: what a joiner may do, and doing it.

A joiner is whoever opens /j/<code> on their phone. If they already have a
caffeination identity they join as themselves (their usual drink, or a
one-off); otherwise they are added as a guest, a one-off person who lives
only in this order. Either way the device gets a signed "join" cookie that
remembers which lines it added, so the joiner can change or remove them
until the buyer sends Ordered, and see the order's progress.

Joins are accepted while the window is open and the order is under its
cap, and each device can add at most JOINER_MAX drinks. The checks and the
insert run under one lock: the app runs a single worker, so that is enough
to stop two people taking the last place at once.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Request, Response
from itsdangerous import BadSignature, URLSafeSerializer
from sqlalchemy import func
from sqlmodel import Session, delete, or_, select

from app.config import get_settings
from app.drinks import DRINK_OPTION_FIELDS, line_drink, upsert_saved_drink
from app.models import Order, OrderItem, PlaceName, SavedDrink, User
from app.sharing import LIVE_STATUSES, _aware
from app.users import create_named_user, touch_last_active

JOINER_MAX = 2  # drinks one device can add: one's own and one for a colleague
FINAL_SHOWN = timedelta(hours=1)  # how long a finished order still answers its code
# How long a device that joined can still look up the order once it has
# finished, to be told that it has (J7). Closed orders are pruned after this.
FINISHED_KEPT = timedelta(days=1)
JOIN_COOKIE = "join"
JOIN_COOKIE_MAX_AGE = 60 * 60 * 24 * 8  # the longest an advance order can run, and a day
GUEST_NAME_MAX = 40
LOCATION_MAX = 60
SUGGESTIONS = 6
SUGGESTION_WINDOW = timedelta(days=90)

_join_lock = threading.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# The join cookie
# ---------------------------------------------------------------------------


@dataclass
class JoinCookie:
    """What this device remembers: its joiner id in each order it joined,
    and the location it last gave, to fill in next time."""

    orders: dict[str, str] = field(default_factory=dict)
    location: Optional[str] = None
    # A guest's own line in each order (the target id), as distinct from a
    # drink they added for a colleague.
    selves: dict[str, str] = field(default_factory=dict)

    def joiner_for(self, order: Order) -> Optional[str]:
        return self.orders.get(order_key(order))

    def self_for(self, order: Order) -> Optional[str]:
        return self.selves.get(order_key(order))


def order_key(order: Order) -> str:
    """How the cookie names an order: its id and when it was created. The id
    alone isn't enough, because SQLite can reuse the id of a pruned order,
    and a device must never take a new order for one it joined before."""
    return f"{order.id}.{int(_aware(order.created_at).timestamp())}"


def _key_id(key: str) -> Optional[int]:
    head = key.split(".", 1)[0]
    return int(head) if head.isdigit() else None


def _serializer() -> URLSafeSerializer:
    return URLSafeSerializer(get_settings().secret_key, salt="join")


def _joiner_serializer() -> URLSafeSerializer:
    return URLSafeSerializer(get_settings().secret_key, salt="joiner")


def joiner_token(joiner_id: str) -> str:
    """A signed joiner id for the guest form of a device that has none yet,
    so that a second tap sent before the first answer (and its cookie)
    arrives is recognised as the same device."""
    return _joiner_serializer().dumps(joiner_id)


def read_joiner_token(token: Optional[str]) -> Optional[str]:
    if not token:
        return None
    try:
        value = _joiner_serializer().loads(token)
    except BadSignature:
        return None
    return value if isinstance(value, str) and value.startswith("g_") else None


def read_join_cookie(request: Request) -> JoinCookie:
    raw = request.cookies.get(JOIN_COOKIE)
    if not raw:
        return JoinCookie()
    try:
        data = _serializer().loads(raw)
    except BadSignature:
        return JoinCookie()
    return JoinCookie(
        orders=dict(data.get("o") or {}), location=data.get("l"), selves=dict(data.get("s") or {})
    )


def write_join_cookie(request: Request, response: Response, cookie: JoinCookie) -> None:
    # Only the last few orders matter; older entries point at orders that
    # have long since finished.
    orders = dict(list(cookie.orders.items())[-10:])
    selves = {k: v for k, v in cookie.selves.items() if k in orders}
    token = _serializer().dumps({"o": orders, "l": cookie.location, "s": selves})
    response.set_cookie(
        JOIN_COOKIE,
        token,
        max_age=JOIN_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )


# ---------------------------------------------------------------------------
# Rate limiting code lookups
# ---------------------------------------------------------------------------


class CodeAttempts:
    """Unknown codes tried per client address in the last minute. Only
    misses count, so a joiner's page refreshing never trips it. Held in
    memory, which works because the app runs one worker."""

    LIMIT = 10
    WINDOW = 60.0

    def __init__(self) -> None:
        self._misses: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _trim(self, key: str, now: float) -> deque[float]:
        misses = self._misses[key]
        while misses and now - misses[0] > self.WINDOW:
            misses.popleft()
        return misses

    def blocked(self, key: str) -> bool:
        with self._lock:
            return len(self._trim(key, time.monotonic())) >= self.LIMIT

    def miss(self, key: str) -> None:
        with self._lock:
            now = time.monotonic()
            self._trim(key, now).append(now)
            if len(self._misses) > 10_000:  # never grows without bound
                self._misses.clear()


# ---------------------------------------------------------------------------
# Finding the order and the joiner
# ---------------------------------------------------------------------------


def find_order_by_code(
    session: Session, code: str, now: Optional[datetime] = None, known: frozenset = frozenset()
) -> Optional[Order]:
    """The live order using this code or, failing that, for a device that
    joined it (its key is in known), one that finished with it less than
    FINISHED_KEPT ago, so that the joiner can be told how it ended. Join
    links are public, so a finished order answers no one else."""
    live = session.exec(
        select(Order).where(Order.code == code, Order.status.in_(LIVE_STATUSES))
    ).first()
    if live is not None:
        return live
    now = now or _now()
    if known:
        # The buyer replaced the code after this device joined; the order
        # may since have finished.
        replaced = session.exec(
            select(Order)
            .where(
                Order.previous_code == code,
                Order.id.in_([i for i in map(_key_id, known) if i is not None]),
            )
            .order_by(Order.id.desc())
        ).first()
        if replaced is not None and order_key(replaced) in known and (
            replaced.closed_at is None or now - _aware(replaced.closed_at) < FINISHED_KEPT
        ):
            return replaced
    recent = session.exec(
        select(Order)
        .where(Order.code == code, Order.closed_at != None)  # noqa: E711
        .order_by(Order.closed_at.desc())
    ).first()
    if recent is None:
        return None
    age = now - _aware(recent.closed_at)
    if order_key(recent) in known and age < FINISHED_KEPT:
        return recent
    return None


def recently_closed(order: Order, now: Optional[datetime] = None) -> bool:
    """Closed less than FINAL_SHOWN ago: joiners are still told how it ended."""
    return order.closed_at is not None and (now or _now()) - _aware(order.closed_at) < FINAL_SHOWN


def identity_user(session: Session, user_id: Optional[str]) -> Optional[User]:
    """The visitor's own caffeination user, if they have named themselves."""
    if user_id is None:
        return None
    user = session.get(User, user_id)
    if user is None or user.display_name is None or user.one_off:
        return None
    return user


@dataclass
class Joiner:
    """Who is looking at the join page, relative to one order."""

    user: Optional[User]  # their own caffeination user, if any
    joiner_id: Optional[str]  # their joiner id in this order, if they joined
    self_id: Optional[str] = None  # a guest's own line (target id), if any

    @property
    def key(self) -> Optional[str]:
        """The id their lines carry in joined_by."""
        return self.joiner_id or (self.user.id if self.user else None)


@dataclass
class OwnLine:
    item: OrderItem
    person: User
    drink: Optional[SavedDrink]
    is_self: bool  # the joiner's own line, not one for a colleague


def own_lines(session: Session, order: Order, joiner: Joiner) -> list[OwnLine]:
    """The lines this joiner may see and change: the ones their device
    added, and their own line however it got there (the buyer may have
    added them from the roster)."""
    conditions = []
    if joiner.key is not None:
        conditions.append(OrderItem.joined_by == joiner.key)
    if joiner.user is not None:
        conditions.append(OrderItem.target_user_id == joiner.user.id)
    if not conditions:
        return []
    rows = session.exec(
        select(OrderItem, User, SavedDrink)
        .join(User, User.id == OrderItem.target_user_id)
        .join(SavedDrink, SavedDrink.user_id == OrderItem.target_user_id, isouter=True)
        .where(OrderItem.order_id == order.id, or_(*conditions))
        .order_by(OrderItem.added_at)
    ).all()
    own_id = joiner.user.id if joiner.user is not None else joiner.self_id
    lines = [
        OwnLine(item, person, line_drink(item, sd), own_id is not None and person.id == own_id)
        for item, person, sd in rows
    ]
    lines.sort(key=lambda line: not line.is_self)
    return lines


def drink_count(session: Session, order: Order) -> int:
    """Drinks in the order: lines with a drink (one-off or usual), plus the
    owner's own unless they have taken themselves out."""
    lines = session.exec(
        select(func.count())
        .select_from(OrderItem)
        .join(SavedDrink, SavedDrink.user_id == OrderItem.target_user_id, isouter=True)
        .where(
            OrderItem.order_id == order.id,
            or_(OrderItem.base_id != None, SavedDrink.user_id != None),  # noqa: E711
        )
    ).one()
    owner = 0
    if order.owner_excluded_at is None and session.get(SavedDrink, order.owner_id) is not None:
        owner = 1
    return lines + owner


# ---------------------------------------------------------------------------
# Whether joins are accepted
# ---------------------------------------------------------------------------

ACCEPTING, CLOSED, FULL, FINISHED = "accepting", "closed", "full", "finished"


def join_state(order: Order, count: int, now: Optional[datetime] = None) -> str:
    """accepting, closed (window over, or the buyer has ordered), full, or
    finished (ready, cancelled or expired)."""
    if order.status not in LIVE_STATUSES:
        return FINISHED
    if order.status != "open" or order.joins_close_at is None:
        return CLOSED
    if _aware(order.joins_close_at) <= (now or _now()):
        return CLOSED
    if count >= order.max_drinks:
        return FULL
    return ACCEPTING


def stop_sharing(session: Session, order: Order) -> bool:
    """Take an open everyday order back to private: no code, no closing time,
    and the sharing settings forgotten, so Share starts afresh. Refused
    (False) once anyone has added a drink by code, because their page finds
    the order by its code; the buyer closes joins or clears the order
    instead. Under the join lock, so a join can't land in between."""
    with _join_lock:
        session.refresh(order)
        joined = session.exec(
            select(func.count())
            .select_from(OrderItem)
            .where(OrderItem.order_id == order.id, OrderItem.joined_by.is_not(None))
        ).one()
        if joined or order.status != "open" or order.kind != "everyday":
            return False
        order.code = None
        order.previous_code = None
        order.joins_close_at = None
        order.name = None
        order.location = None
        order.max_drinks = Order.model_fields["max_drinks"].default
        session.add(order)
        session.commit()
        return True


def lines_editable(order: Order) -> bool:
    """Joiners can change or remove their lines until the buyer sends Ordered."""
    return order.status == "open"


class JoinRefused(Exception):
    """A join that can't go ahead; reason is one of CLOSED, FULL, FINISHED,
    "joiner_max" or "already"."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------------------
# Joining
# ---------------------------------------------------------------------------


def clean_location(raw: Optional[str]) -> Optional[str]:
    text = " ".join((raw or "").split())[:LOCATION_MAX]
    return text or None


def _check_can_add(session: Session, order: Order, joiner_key: str) -> None:
    state = join_state(order, drink_count(session, order))
    if state != ACCEPTING:
        raise JoinRefused(state)
    added = session.exec(
        select(func.count())
        .select_from(OrderItem)
        .where(OrderItem.order_id == order.id, OrderItem.joined_by == joiner_key)
    ).one()
    if added >= JOINER_MAX:
        raise JoinRefused("joiner_max")


def _record_place(session: Session, location: Optional[str]) -> None:
    if location is None:
        return
    key = location.casefold()
    place = session.get(PlaceName, key)
    if place is None:
        place = PlaceName(key=key, name=location)
    place.name = location
    place.uses += 1
    place.last_used_at = _now()
    session.add(place)


def join_as_self(
    session: Session,
    order: Order,
    user: User,
    *,
    location: Optional[str],
    drink: Optional[dict] = None,
    save_as_usual: bool = False,
) -> None:
    """Add the joiner's own line: their usual drink, or a one-off (drink).
    With save_as_usual the drink becomes their usual and the line uses it."""
    with _join_lock:
        # The order was loaded before the form was read; the buyer may have
        # closed joins, sent Ordered or changed the cap since.
        session.refresh(order)
        if session.get(OrderItem, (order.id, user.id)) is not None:
            raise JoinRefused("already")
        _check_can_add(session, order, user.id)
        if drink is not None and save_as_usual:
            upsert_saved_drink(session, user.id, drink)
            drink = None
        if drink is None and session.get(SavedDrink, user.id) is None:
            raise JoinRefused("no_drink")
        item = OrderItem(
            order_id=order.id,
            target_user_id=user.id,
            joined_by=user.id,
            location=None if order.location else location,
            **(drink or {}),
        )
        session.add(item)
        _record_place(session, item.location)
        session.commit()
    touch_last_active(session, user)


def join_as_guest(
    session: Session,
    order: Order,
    joiner_key: str,
    *,
    name: str,
    drink: dict,
    location: Optional[str],
) -> User:
    """Add a guest: a one-off person, made by the order's buyer for the
    purposes of clean-up, who lives only in this order."""
    name = name[:GUEST_NAME_MAX]
    with _join_lock:
        session.refresh(order)  # as in join_as_self
        repeat = _same_guest(session, order, joiner_key, name, drink)
        if repeat is not None:
            # The same form sent twice (a double tap, or a resend): the
            # first one already added this drink.
            return repeat
        _check_can_add(session, order, joiner_key)
        guest = create_named_user(
            session, name, created_by=order.owner_id, one_off=True
        )
        upsert_saved_drink(session, guest.id, drink)
        item = OrderItem(
            order_id=order.id,
            target_user_id=guest.id,
            joined_by=joiner_key,
            location=None if order.location else location,
        )
        session.add(item)
        _record_place(session, item.location)
        session.commit()
    return guest


def _same_guest(
    session: Session, order: Order, joiner_key: str, name: str, drink: dict
) -> Optional[User]:
    """A guest this joiner already added to the order with this name and
    drink, if any."""
    rows = session.exec(
        select(User, SavedDrink)
        .join(OrderItem, OrderItem.target_user_id == User.id)
        .join(SavedDrink, SavedDrink.user_id == User.id)
        .where(
            OrderItem.order_id == order.id,
            OrderItem.joined_by == joiner_key,
            OrderItem.base_id == None,  # noqa: E711
            User.one_off == True,  # noqa: E712
            User.display_name == name,
        )
    ).all()
    for guest, saved in rows:
        if all(getattr(saved, f) == v for f, v in drink.items()):
            return guest
    return None


def _owned_item(session: Session, order: Order, joiner: Joiner, target_id: str) -> Optional[OrderItem]:
    for line in own_lines(session, order, joiner):
        if line.item.target_user_id == target_id:
            return line.item
    return None


def set_line_location(
    session: Session, order: Order, joiner: Joiner, target_id: str, location: Optional[str]
) -> bool:
    if not lines_editable(order) or order.location:
        return False
    item = _owned_item(session, order, joiner, target_id)
    if item is None:
        return False
    item.location = location
    session.add(item)
    _record_place(session, location)
    session.commit()
    return True


def set_line_drink(
    session: Session, order: Order, joiner: Joiner, target_id: str, drink: Optional[dict]
) -> bool:
    """Swap the joiner's own line to a one-off drink, or back to their usual
    (drink None)."""
    if not lines_editable(order):
        return False
    item = _owned_item(session, order, joiner, target_id)
    if item is None or joiner.user is None or item.target_user_id != joiner.user.id:
        return False
    for f in DRINK_OPTION_FIELDS:
        setattr(item, f, (drink or {}).get(f))
    session.add(item)
    session.commit()
    return True


def set_guest_drink(
    session: Session, order: Order, joiner: Joiner, target_id: str, drink: dict
) -> bool:
    """Change a guest's drink. A guest has no usual drink to fall back on,
    so their own drink is changed rather than swapped for a one-off."""
    if not lines_editable(order):
        return False
    item = _owned_item(session, order, joiner, target_id)
    guest = session.get(User, target_id) if item is not None else None
    if guest is None or not guest.one_off:
        return False
    upsert_saved_drink(session, guest.id, drink)
    session.commit()
    return True


def remove_line(session: Session, order: Order, joiner: Joiner, target_id: str) -> bool:
    """Remove one of the joiner's lines; a guest goes with their line."""
    if not lines_editable(order):
        return False
    item = _owned_item(session, order, joiner, target_id)
    if item is None:
        return False
    session.delete(item)
    target = session.get(User, target_id)
    if target is not None and target.one_off:
        session.exec(delete(SavedDrink).where(SavedDrink.user_id == target_id))
        session.delete(target)
    session.commit()
    return True


def new_joiner_id() -> str:
    return "g_" + secrets.token_urlsafe(9)


def suggested_places(session: Session, now: Optional[datetime] = None) -> list[str]:
    """The department's most used locations lately, for the location field."""
    cutoff = (now or _now()) - SUGGESTION_WINDOW
    return list(
        session.exec(
            select(PlaceName.name)
            .where(PlaceName.last_used_at >= cutoff)
            .order_by(PlaceName.uses.desc(), PlaceName.name)
            .limit(SUGGESTIONS)
        ).all()
    )
