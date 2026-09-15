"""Per-request measurement: a Server-Timing header on every response.

Open the browser's Network panel, pick a request, and the Timing tab shows
``app`` (time inside the ASGI app, template render included) and ``db``
(number of SQL statements the request issued). Cheap enough to leave on in
production, and it's the only honest way to see what a route costs on the
real database rather than a seeded one. scripts/bench.py reads the same
header.
"""

from __future__ import annotations

from time import perf_counter

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.db import query_count, start_query_count


class ServerTimingMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        start_query_count()
        t0 = perf_counter()

        async def send_with_timing(message: Message) -> None:
            if message["type"] == "http.response.start":
                elapsed_ms = (perf_counter() - t0) * 1000
                queries = query_count() or 0
                MutableHeaders(scope=message).append(
                    "server-timing",
                    f"app;dur={elapsed_ms:.1f}, db;desc=\"{queries} queries\"",
                )
            await send(message)

        await self.app(scope, receive, send_with_timing)
