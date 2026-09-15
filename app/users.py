import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, Request
from sqlalchemy import func
from sqlmodel import Session, delete, select, update

from app.db import get_session
from app.drinks import LINE_FIELDS, format_line
from app.identity import mint_identity, read_identity, set_identity
from app.models import OrderItem, SavedDrink, User

# How long after creating a roster person you may still edit their usual.
EDIT_WINDOW = timedelta(hours=2)

# Visits refresh last_active_at at most this often, so a busy session isn't a
# write per request. Any gap under a day is far finer than the roster's
# 90-day activity window needs.
TOUCH_INTERVAL = timedelta(hours=1)

# The discoverable roster: named, non-one-off users. One-off guest entries
# are deliberately excluded from search/picker/uniqueness. Reused by every
# roster query so the definition lives in one place.
ROSTER_FILTER = (User.display_name.is_not(None), User.one_off == False)  # noqa: E712


def touch_last_active(
    session: Session, user: User, now: Optional[datetime] = None
) -> None:
    """Record a sign of life (visit, drink save, being added to an order)."""
    now = now or datetime.now(timezone.utc)
    last = user.last_active_at
    if last is not None and _as_naive_utc(now) - _as_naive_utc(last) < TOUCH_INTERVAL:
        return
    user.last_active_at = now
    session.add(user)
    session.commit()


def get_current_user(
    request: Request,
    session: Session = Depends(get_session),
) -> User:
    """Resolve (or mint) the user behind the signed identity cookie.

    First visits stash a fresh signed token on `request.state`; the
    identity middleware writes the Set-Cookie header on the way out.

    Unknown ids get a TRANSIENT User (not persisted) — a row is only
    written once the visitor names themselves (or claims someone), so
    cookie-less bots and bounced visits don't litter the table.
    """
    user_id = read_identity(request)
    if user_id is None:
        user_id = mint_identity(request)
    user = session.get(User, user_id)
    if user is None:
        user = User(id=user_id)
    else:
        touch_last_active(session, user)
    return user


def _roster_names(session: Session) -> list[tuple[str, str]]:
    """(id, display_name) for every roster user — columns only, no ORM
    hydration, since the callers just compare names."""
    return session.exec(
        select(User.id, User.display_name).where(*ROSTER_FILTER)
    ).all()


def find_user_by_display_name(session: Session, name: str) -> Optional[User]:
    """Case-insensitive roster lookup.

    The comparison stays in Python on purpose: SQLite's lower()/NOCASE only
    fold ASCII, so pushing it into the WHERE clause would let "Éamonn" and
    "éamonn" coexist. The roster is a few hundred short strings at most.
    """
    target = name.strip().lower()
    for user_id, display_name in _roster_names(session):
        if display_name.lower() == target:
            return session.get(User, user_id)
    return None


def existing_names_lower(session: Session) -> list[str]:
    """Every taken roster name, lowercased. Feeds the client-side dup check."""
    return [display_name.lower() for _, display_name in _roster_names(session)]


def create_named_user(
    session: Session,
    name: str,
    *,
    created_by: Optional[str] = None,
    one_off: bool = False,
) -> User:
    """Create a brand-new named user (not bound to anyone's cookie).

    For roster users, callers must validate uniqueness via
    `find_user_by_display_name` first. One-off users skip that check.
    """
    user = User(
        id=secrets.token_urlsafe(12),
        display_name=name,
        created_by=created_by,
        one_off=one_off,
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _as_naive_utc(dt: datetime) -> datetime:
    """SQLite drops tzinfo on write, so a row read back is naive UTC while a
    freshly-built object is aware. Normalise both to naive UTC for comparison."""
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def can_edit_person(owner_id: str, target: Optional[User], now: Optional[datetime] = None) -> bool:
    """Whether `owner_id` may edit `target`'s usual.

    Only the creator can edit. One-offs stay editable for their whole life;
    roster people are editable for EDIT_WINDOW after creation, then locked.
    """
    if target is None or target.created_by != owner_id:
        return False
    if target.one_off:
        return True
    now = now or datetime.now(timezone.utc)
    return (_as_naive_utc(now) - _as_naive_utc(target.created_at)) < EDIT_WINDOW


def named_users_with_lines(session: Session) -> list[dict]:
    """All roster users, with their saved-drink line. For the onboarding picker.

    One LEFT JOIN of just the columns needed, ordered in SQL; users without
    a drink get an empty line.
    """
    rows = session.exec(
        select(User.id, User.display_name, *(getattr(SavedDrink, f) for f in LINE_FIELDS))
        .join(SavedDrink, SavedDrink.user_id == User.id, isouter=True)
        .where(*ROSTER_FILTER)
        .order_by(func.lower(User.display_name))
    ).all()
    return [
        {
            "id": uid,
            "display_name": name,
            "drink_line": format_line(*drink) if drink[0] is not None else "",
        }
        for uid, name, *drink in rows
    ]


def delete_user(session: Session, user_id: str) -> None:
    """Remove a user and everything hanging off them.

    Their own open order (including any one-off guests it spawned), their
    presence in other people's orders, and their saved drink all go; people
    they created stay on the roster with `created_by` detached. Set-based
    statements, one commit.
    """
    from app.orders import clear_order  # function-local: orders imports us

    user = session.get(User, user_id)
    if user is None:
        return
    clear_order(session, user_id)
    session.exec(delete(OrderItem).where(OrderItem.target_user_id == user_id))
    session.exec(
        update(User).where(User.created_by == user_id).values(created_by=None)
    )
    session.exec(delete(SavedDrink).where(SavedDrink.user_id == user_id))
    session.delete(user)
    session.commit()


def claim_user(
    session: Session,
    request: Request,
    target_user_id: str,
) -> Optional[User]:
    """Rebind the cookie to an existing named user.

    Refuses if the target doesn't exist, has no display_name, or is a
    one-off guest — one-offs live only inside their creator's order and
    are hard-deleted when removed from it, so a cookie must never point
    at one. (Unnamed visitors are never persisted, so there's no orphan
    row to clean up here.)
    """
    target = session.get(User, target_user_id)
    if target is None or target.display_name is None or target.one_off:
        return None
    set_identity(request, target.id)
    return target
