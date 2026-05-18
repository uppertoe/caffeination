from typing import Optional

from fastapi import Depends, Request
from sqlmodel import Session, select

from app.db import get_session
from app.drinks import format_drink, get_saved_drink
from app.identity import mint_identity, read_identity, set_identity
from app.menu import get_drink
from app.models import SavedDrink, User


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
    return user


def find_user_by_display_name(session: Session, name: str) -> Optional[User]:
    target = name.strip().lower()
    for u in session.exec(select(User).where(User.display_name.is_not(None))).all():
        if u.display_name.lower() == target:
            return u
    return None


def named_users_with_lines(session: Session) -> list[dict]:
    """All named users, with their saved-drink line. For the onboarding picker."""
    users = session.exec(select(User).where(User.display_name.is_not(None))).all()
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
