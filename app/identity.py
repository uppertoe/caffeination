"""Cookie-backed identity.

A signed, opaque user id is set on first visit. The dependency
`get_current_user` reads it (or mints a new one); a middleware on the
response side actually writes the Set-Cookie header.

We use the middleware split because FastAPI does NOT merge cookies set on
a dependency-injected `Response` into a returned `TemplateResponse` — the
temporal response is discarded when the handler returns its own Response.
"""

from __future__ import annotations

import secrets

from fastapi import Request, Response
from itsdangerous import BadSignature, URLSafeSerializer

from app.config import get_settings

_FRESH_TOKEN_ATTR = "fresh_identity_token"


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


def set_identity(request: Request, user_id: str) -> None:
    """Stash a signed cookie value for the identity middleware to write."""
    setattr(request.state, _FRESH_TOKEN_ATTR, _serializer().dumps(user_id))


def mint_identity(request: Request) -> str:
    """Generate a new identity and stash the signed token for the middleware."""
    user_id = secrets.token_urlsafe(12)
    set_identity(request, user_id)
    return user_id


def apply_fresh_identity(request: Request, response: Response) -> None:
    token = getattr(request.state, _FRESH_TOKEN_ATTR, None)
    if not token:
        return
    settings = get_settings()
    response.set_cookie(
        key=settings.cookie_name,
        value=token,
        max_age=settings.cookie_max_age,
        httponly=True,
        samesite="lax",
        # Only mark Secure when actually served over HTTPS so local http
        # development and the test client (http://testserver) still work.
        secure=request.url.scheme == "https",
    )
