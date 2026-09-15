"""Cookie-backed identity.

A signed, opaque user id is set on first visit. The dependency
`get_current_user` reads it (or mints a new one); `IdentityMiddleware` on
the response side actually writes the Set-Cookie header.

We use the middleware split because FastAPI does NOT merge cookies set on
a dependency-injected `Response` into a returned `TemplateResponse` — the
temporal response is discarded when the handler returns its own Response.

The middleware is a plain ASGI callable rather than Starlette's
BaseHTTPMiddleware: the base class re-wraps every response as a stream
and spins up a task group per request, which costs more than the app's
own work on the cheap routes and buries the handler's context in a
separate task.
"""

from __future__ import annotations

import secrets

from fastapi import Request, Response
from itsdangerous import BadSignature, URLSafeSerializer
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

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


def _cookie_header(token: str, scheme: str) -> str:
    """Build the Set-Cookie value via Starlette's own cookie serialiser."""
    settings = get_settings()
    scratch = Response()
    scratch.set_cookie(
        key=settings.cookie_name,
        value=token,
        max_age=settings.cookie_max_age,
        httponly=True,
        samesite="lax",
        # Only mark Secure when actually served over HTTPS so local http
        # development and the test client (http://testserver) still work.
        secure=scheme == "https",
    )
    return scratch.headers["set-cookie"]


class IdentityMiddleware:
    """Write the Set-Cookie header if `get_current_user` minted a fresh id.

    `request.state` is a view over `scope["state"]`, so the token stashed by
    the dependency is readable here without a Request object.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_cookie(message: Message) -> None:
            if message["type"] == "http.response.start":
                token = scope.get("state", {}).get(_FRESH_TOKEN_ATTR)
                if token:
                    MutableHeaders(scope=message).append(
                        "set-cookie", _cookie_header(token, scope.get("scheme", "http"))
                    )
            await send(message)

        await self.app(scope, receive, send_with_cookie)
