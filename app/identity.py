"""Cookie-backed identity.

We hand out a signed, opaque user id on first visit and use it to look up
(or create) a user row. No passwords. Future work: wire to a User model and
let the user set a display name on the next request.
"""

from __future__ import annotations

import secrets

from fastapi import Request, Response
from itsdangerous import BadSignature, URLSafeSerializer

from app.config import get_settings


def _serializer() -> URLSafeSerializer:
    return URLSafeSerializer(get_settings().secret_key, salt="identity")


def read_identity(request: Request) -> str | None:
    raw = request.cookies.get(get_settings().cookie_name)
    if not raw:
        return None
    try:
        return _serializer().loads(raw)
    except BadSignature:
        return None


def issue_identity(response: Response) -> str:
    settings = get_settings()
    user_id = secrets.token_urlsafe(12)
    response.set_cookie(
        key=settings.cookie_name,
        value=_serializer().dumps(user_id),
        max_age=settings.cookie_max_age,
        httponly=True,
        samesite="lax",
        secure=not settings.debug,
    )
    return user_id
