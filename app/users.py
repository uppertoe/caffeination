import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, Request
from sqlmodel import Session, select

from app.db import get_session
from app.drinks import format_drink, get_saved_drink
from app.identity import mint_identity, read_identity, set_identity
from app.menu import get_drink
from app.models import OrderItem, SavedDrink, User

# How long after creating a roster person you may still edit their usual.
EDIT_WINDOW = timedelta(hours=2)

# Visits refresh last_active_at at most this often, so a busy session isn't a
# write per request. Any gap under a day is far finer than the roster's
# 90-day activity window needs.
TOUCH_INTERVAL = timedelta(hours=1)


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
    """
    user_id = read_identity(request)
    if user_id is None:
        user_id = mint_identity(request)
    user = session.get(User, user_id)
    if user is None:
        user = User(id=user_id)
        session.add(user)
        session.commit()
        session.refresh(user)
    else:
        touch_last_active(session, user)
    return user


def _roster_users(session: Session) -> list[User]:
    """Named, non-one-off users — the discoverable roster. One-off guest
    entries are deliberately excluded from search/picker/uniqueness."""
    return session.exec(
        select(User).where(User.display_name.is_not(None), User.one_off == False)  # noqa: E712
    ).all()


def find_user_by_display_name(session: Session, name: str) -> Optional[User]:
    target = name.strip().lower()
    for u in _roster_users(session):
        if u.display_name.lower() == target:
            return u
    return None


def existing_names_lower(session: Session) -> list[str]:
    """Every taken roster name, lowercased. Feeds the client-side dup check."""
    return [u.display_name.lower() for u in _roster_users(session)]


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
    """All roster users, with their saved-drink line. For the onboarding picker."""
    users = _roster_users(session)
    drinks = {sd.user_id: sd for sd in session.exec(select(SavedDrink)).all()}
    out: list[dict] = []
    for u in users:
        sd = drinks.get(u.id)
        if sd is not None:
            drink = get_drink(sd.base_id)
            line = format_drink(drink, sd) if drink else sd.base_id
        else:
            line = ""
        out.append({"id": u.id, "display_name": u.display_name, "drink_line": line})
    out.sort(key=lambda x: x["display_name"].lower())
    return out


def delete_user(session: Session, user_id: str) -> None:
    """Remove a user and everything hanging off them.

    Their own open order (including any one-off guests it spawned), their
    presence in other people's orders, and their saved drink all go; people
    they created stay on the roster with `created_by` detached.
    """
    from app.orders import clear_order  # function-local: orders imports us

    user = session.get(User, user_id)
    if user is None:
        return
    clear_order(session, user_id)
    for item in session.exec(
        select(OrderItem).where(OrderItem.target_user_id == user_id)
    ).all():
        session.delete(item)
    for created in session.exec(
        select(User).where(User.created_by == user_id)
    ).all():
        created.created_by = None
        session.add(created)
    saved = session.get(SavedDrink, user_id)
    if saved is not None:
        session.delete(saved)
    session.delete(user)
    session.commit()


def claim_user(
    session: Session,
    request: Request,
    current_user: User,
    target_user_id: str,
) -> Optional[User]:
    """Rebind the cookie to an existing named user.

    Refuses if the target doesn't exist or has no display_name. If the
    current user row is empty (no display_name), it's deleted so we don't
    leave orphan rows behind.
    """
    target = session.get(User, target_user_id)
    if target is None or target.display_name is None:
        return None
    if current_user.id != target.id and current_user.display_name is None:
        session.delete(current_user)
        session.commit()
    set_identity(request, target.id)
    return target
