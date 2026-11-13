"""Sharing an order: the join code, its QR code, the join window and the cap.

A shared order has a six-letter code that colleagues use to add their own
drink (app.joining). Joins are accepted
while the window is open and the order has fewer drinks than its cap. Both
are worked out at request time from the Order row, so nothing has to move
an order between states.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Optional
from zoneinfo import ZoneInfo

import segno
from markupsafe import Markup
from sqlmodel import Session, select

from app.config import get_settings
from app.models import LIVE_STATUSES, Order
from app.users import _as_naive_utc

# Consonants only, so a code can't spell a word, without L (read as I or 1)
# or Y. 19^6 is about 47 million codes, of which a handful are live at once.
CODE_ALPHABET = "BCDFGHJKMNPQRSTVWXZ"
CODE_LENGTH = 6

JOIN_WINDOW = timedelta(minutes=30)  # the default, when sharing at once
EXTEND_BY = timedelta(minutes=15)  # "Allow 15 more minutes" / "Reopen"
MAX_AHEAD = timedelta(days=7)  # furthest a chosen closing time can be
DEFAULT_MAX_DRINKS = 8  # about as many as one person can carry
MIN_MAX_DRINKS, MAX_MAX_DRINKS = 1, 40
NAME_MAX = 60
LOCATION_MAX = 60
MESSAGE_MAX = 140

# The updates a buyer can send, in the order they happen (O5).
UPDATE_STATUSES = ("ordered", "on_the_way", "ready")
UPDATE_LABELS = {"ordered": "Ordered", "on_the_way": "On the way", "ready": "Ready"}



def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime) -> datetime:
    """SQLite hands datetimes back naive; they are always UTC."""
    return _as_naive_utc(dt).replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Codes
# ---------------------------------------------------------------------------


def normalise_code(raw: str) -> Optional[str]:
    """The code in canonical form (upper case, no spaces), or None if the
    input can't be one. Accepts any case, spaces and hyphens."""
    code = "".join(ch for ch in (raw or "").upper() if ch not in " -")
    if len(code) != CODE_LENGTH or any(ch not in CODE_ALPHABET for ch in code):
        return None
    return code


def format_code(code: str) -> str:
    """How a code is shown: two groups of three, "BKT WQR"."""
    return f"{code[:3]} {code[3:]}"


def _random_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def _new_code(session: Session) -> str:
    """A code no live order is using. A clash is about one in several
    million while a handful of orders are live, so this loops at most once
    or twice in practice; the unique index is the real guarantee."""
    while True:
        code = _random_code()
        taken = session.exec(
            select(Order.id).where(Order.code == code, Order.status.in_(LIVE_STATUSES))
        ).first()
        if taken is None:
            return code


def join_path(code: str) -> str:
    return f"/j/{code}"


@lru_cache(maxsize=64)
def qr_svg(url: str) -> Markup:
    """The QR code for a join URL as inline SVG, sized by CSS. Always dark
    on light whatever the theme, because that is what phone cameras read
    most reliably."""
    svg = segno.make(url, error="m").svg_inline(
        scale=1, border=2, dark="#000", light="#fff", omitsize=True
    )
    return Markup(svg)


# ---------------------------------------------------------------------------
# Changing how an order is shared
# ---------------------------------------------------------------------------


class ShareError(ValueError):
    """A sharing form that can't be accepted; the message is shown as is."""


def _clean_text(value: Optional[str], limit: int) -> Optional[str]:
    text = " ".join((value or "").split())[:limit]
    return text or None


def parse_closing_time(raw: str, now: Optional[datetime] = None, *, past_is_now: bool = False) -> datetime:
    """A datetime-local value ("2026-11-10T16:00"), read as local time, as
    UTC. Raises ShareError if it's missing, in the past or too far ahead;
    with past_is_now, a time in the past is taken as now instead."""
    now = now or _now()
    try:
        local = datetime.fromisoformat((raw or "").strip())
    except ValueError:
        raise ShareError("Please choose a closing time.") from None
    if local.tzinfo is None:
        local = local.replace(tzinfo=ZoneInfo(get_settings().timezone))
    when = local.astimezone(timezone.utc)
    if when <= now:
        if past_is_now:
            return now
        raise ShareError("Please choose a closing time in the future.")
    if when > now + MAX_AHEAD:
        raise ShareError("The closing time can be at most seven days ahead.")
    return when


def clamp_max_drinks(raw) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_DRINKS
    return max(MIN_MAX_DRINKS, min(MAX_MAX_DRINKS, value))


def share_order(
    session: Session,
    order: Order,
    *,
    closes_at: Optional[datetime] = None,
    name: Optional[str] = None,
    location: Optional[str] = None,
    max_drinks: int = DEFAULT_MAX_DRINKS,
) -> None:
    """Give the order a code and open it to joins until closes_at (by
    default, JOIN_WINDOW from now)."""
    if order.code is None:
        order.code = _new_code(session)
    order.joins_close_at = closes_at or _now() + JOIN_WINDOW
    order.name = _clean_text(name, NAME_MAX)
    order.location = _clean_text(location, LOCATION_MAX)
    order.max_drinks = clamp_max_drinks(max_drinks)
    session.add(order)
    session.commit()


def extend_joins(session: Session, order: Order) -> None:
    """Fifteen more minutes from the later of now and the current closing
    time, so the same button extends an open window and reopens a closed one."""
    now = _now()
    current = _aware(order.joins_close_at) if order.joins_close_at else now
    order.joins_close_at = max(now, current) + EXTEND_BY
    session.add(order)
    session.commit()


def close_joins(session: Session, order: Order) -> None:
    """Lock the order: no new drinks, and no changes to those already in.
    Usual drinks are fixed as they stand, as Ordered used to do."""
    _fix_usual_drinks(session, order)
    order.joins_close_at = _now()
    session.add(order)
    session.commit()


def replace_code(session: Session, order: Order) -> None:
    """A fresh code, which stops the old one working at once for anyone
    who has not joined yet. Devices that joined keep their page."""
    order.previous_code = order.code
    order.code = _new_code(session)
    session.add(order)
    session.commit()


def set_closing_time(session: Session, order: Order, raw: str) -> Optional[str]:
    """Move the closing time of an advance order. A time already past
    closes joins at once. Returns an error message, or None."""
    try:
        when = parse_closing_time(raw, past_is_now=True)
    except ShareError as exc:
        return str(exc)
    order.joins_close_at = when
    session.add(order)
    session.commit()
    return None


def set_max_drinks(session: Session, order: Order, value) -> None:
    order.max_drinks = clamp_max_drinks(value)
    session.add(order)
    session.commit()


def _fix_usual_drinks(session: Session, order: Order) -> None:
    """Copy each usual-drink line's drink onto the line, in one statement,
    so that what was ordered no longer follows changes to the usual."""
    from sqlalchemy import exists, update

    from app.drinks import DRINK_OPTION_FIELDS
    from app.models import OrderItem, SavedDrink

    def usual(f):
        return (
            select(getattr(SavedDrink, f))
            .where(SavedDrink.user_id == OrderItem.target_user_id)
            .scalar_subquery()
        )

    session.exec(
        update(OrderItem)
        .where(
            OrderItem.order_id == order.id,
            OrderItem.base_id == None,  # noqa: E711
            exists().where(SavedDrink.user_id == OrderItem.target_user_id),
        )
        .values({**{f: usual(f) for f in DRINK_OPTION_FIELDS}, "drink_fixed": True})
    )


def send_update(session: Session, order: Order, status: str, message: Optional[str]) -> bool:
    """The buyer's update to everyone who joined: Ordered, On the way or
    Ready, with an optional message that replaces any earlier one.

    Ordered closes joins and locks the lines. An update never moves an order
    backwards, though the same status can be sent again with a new message.
    Ready completes the order: it is recorded as a round and closed, keeping
    its lines so that joiners can still see their drink. Returns False if
    the update can't be applied (an unknown status, or one already passed).
    """
    from app.orders import READY, close_order  # function-local: orders is heavier

    if status not in UPDATE_STATUSES or order.status not in LIVE_STATUSES:
        return False
    current = UPDATE_STATUSES.index(order.status) if order.status in UPDATE_STATUSES else -1
    if UPDATE_STATUSES.index(status) < current:
        return False
    if order.status == "open":
        _fix_usual_drinks(session, order)
    # Joins close because the status is no longer open; the closing time is
    # left alone, since the order's expiry is measured from it.
    order.status_message = _clean_text(message, MESSAGE_MAX)
    order.status_at = _now()
    if status == READY:
        close_order(session, order, READY)  # commits
        return True
    order.status = status
    session.add(order)
    session.commit()
    return True


# ---------------------------------------------------------------------------
# What the buyer's sharing panel shows
# ---------------------------------------------------------------------------


def format_time(when: datetime, now: Optional[datetime] = None) -> str:
    """A time as the department reads it: "10:42 am" today, otherwise with
    the day, "4:00 pm, Mon 10 Nov"."""
    tz = ZoneInfo(get_settings().timezone)
    local = _aware(when).astimezone(tz)
    today = _aware(now or _now()).astimezone(tz).date()
    clock = f"{local.hour % 12 or 12}:{local.minute:02d} {'am' if local.hour < 12 else 'pm'}"
    if local.date() == today:
        return clock
    return f"{clock}, {local:%a} {local.day} {local:%b}"


def format_until(when: datetime, now: Optional[datetime] = None) -> str:
    """A closing time for a sentence: "10:42 am" today, otherwise "4:00 pm
    on Monday 10 November"."""
    tz = ZoneInfo(get_settings().timezone)
    local = _aware(when).astimezone(tz)
    today = _aware(now or _now()).astimezone(tz).date()
    clock = f"{local.hour % 12 or 12}:{local.minute:02d} {'am' if local.hour < 12 else 'pm'}"
    if local.date() == today:
        return clock
    return f"{clock} on {local:%A} {local.day} {local:%B}"


_NUMBER_WORDS = (
    "no", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten", "eleven", "twelve",
)


def number_word(n: int) -> str:
    """Small counts in words, as they read in a sentence; larger ones as digits."""
    return _NUMBER_WORDS[n] if 0 <= n < len(_NUMBER_WORDS) else str(n)


@dataclass
class ShareView:
    code: str
    code_display: str
    join_path: str
    closes_label: str  # "10:42 am" or "4:00 pm, Mon 10 Nov"
    closes_sentence: str  # "10:42 am" or "4:00 pm on Monday 10 November"
    drink_count: int
    max_drinks: int
    by_code: int
    name: Optional[str]
    location: Optional[str]
    closed: bool  # the window has ended, or the order has been placed
    full: bool  # the cap is reached
    default_cap: bool
    status: str = "open"
    kind: str = "everyday"
    closes_input: str = ""  # the closing time as a datetime-local value
    status_label: Optional[str] = None  # "Ordered at 10:50 am", once placed
    status_message: Optional[str] = None
    # The updates the buyer can still send: the current status and later ones.
    update_options: tuple = ()

    @property
    def accepting(self) -> bool:
        return not self.closed and not self.full

    @property
    def placed(self) -> bool:
        """Ordered or on the way: joins are over and the lines are locked."""
        return self.status in ("ordered", "on_the_way")


def share_view(
    order: Optional[Order], drink_count: int, by_code: int = 0, now: Optional[datetime] = None
) -> Optional[ShareView]:
    """The sharing panel's facts for an order, or None if it isn't shared."""
    if order is None or order.code is None or order.joins_close_at is None:
        return None
    now = now or _now()
    status_label = None
    if order.status in ("ordered", "on_the_way") and order.status_at is not None:
        at = format_time(order.status_at, now)
        status_label = f"Ordered at {at}" if order.status == "ordered" else f"On the way since {at}"
    first = UPDATE_STATUSES.index(order.status) if order.status in UPDATE_STATUSES else 0
    return ShareView(
        kind=order.kind,
        closes_input=local_input_value(order.joins_close_at),
        status=order.status,
        status_label=status_label,
        status_message=order.status_message,
        update_options=tuple(
            (value, UPDATE_LABELS[value]) for value in UPDATE_STATUSES[first:]
        ),
        code=order.code,
        code_display=format_code(order.code),
        join_path=join_path(order.code),
        closes_label=format_time(order.joins_close_at, now),
        closes_sentence=format_until(order.joins_close_at, now),
        drink_count=drink_count,
        max_drinks=order.max_drinks,
        by_code=by_code,
        name=order.name,
        location=order.location,
        closed=_aware(order.joins_close_at) <= now or order.status != "open",
        full=drink_count >= order.max_drinks,
        default_cap=order.max_drinks == DEFAULT_MAX_DRINKS,
    )


def local_input_value(when: datetime) -> str:
    """A datetime as a datetime-local input value, in local time."""
    local = _aware(when).astimezone(ZoneInfo(get_settings().timezone))
    return local.strftime("%Y-%m-%dT%H:%M")
