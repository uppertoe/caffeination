from fastapi import Depends, Request
from sqlmodel import Session

from app.db import get_session
from app.identity import mint_identity, read_identity
from app.models import User


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
