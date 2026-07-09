---
name: stack-patterns
description: Use this skill when writing or reviewing FastAPI/Jinja2/HTMX/Alpine/SQLModel code in coffee-rch. It captures the non-obvious patterns and gotchas that the first build hit — cookie-via-middleware, lazy SQLite engine, JSON-in-attribute quoting, OOB swaps, server-side normalization. Pair with [[live-build-playbook]] for the build sequence.
---

# stack-patterns

Concrete code-level patterns for this stack. Each section is a one-shot recipe you can apply without rereading the explanation.

## Cookies set in a dependency are silently dropped

**Symptom.** Your `get_current_user(request, response, ...)` dependency calls `response.set_cookie(...)` but the browser never sees the cookie. The first request's response has no `Set-Cookie` header.

**Cause.** FastAPI maintains a "temporal" Response object that it merges into the route's return — but ONLY when the route returns a non-Response value (dict, Pydantic model). When the route returns its own Response (which `TemplateResponse` is), the temporal one is discarded entirely. Cookies you set on it go with it.

**Fix.** Stash the signed token on `request.state` in the dependency; let a middleware write the cookie on the way out.

```python
# app/identity.py
def mint_identity(request: Request) -> str:
    user_id = secrets.token_urlsafe(12)
    request.state.fresh_identity_token = _serializer().dumps(user_id)
    return user_id

def apply_fresh_identity(request: Request, response: Response) -> None:
    token = getattr(request.state, "fresh_identity_token", None)
    if not token:
        return
    response.set_cookie(
        key=settings.cookie_name,
        value=token,
        max_age=settings.cookie_max_age,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )

# app/main.py
class IdentityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        apply_fresh_identity(request, response)
        return response

app.add_middleware(IdentityMiddleware)
```

`set_identity(request, user_id)` is the same trick but with a chosen id — used by the claim flow to rebind a cookie to an existing user.

## Secure cookie flag and the test client

**Symptom.** Tests pass under DEBUG=true but fail in CI; or the second request from the TestClient acts like the first because it's not sending the cookie back.

**Cause.** `secure=True` cookies only travel over https. `http://testserver` (TestClient) and `http://localhost` (uvicorn dev) are http, so the browser/client never sends them.

**Fix.** Key Secure off `request.url.scheme == "https"` instead of an env flag. Works in dev, tests, and production behind a TLS terminator — and removes the DEBUG hack.

## Lazy SQLite engine so tests can swap DATABASE_URL

**Symptom.** Tests get `sqlite3.OperationalError: attempt to write a readonly database` or write to the wrong DB file despite the conftest setting `DATABASE_URL`.

**Cause.** The engine is created at module-import time using whatever `DATABASE_URL` was set then. The conftest fixture sets the env var *after* imports have already happened.

**Fix.** Wrap engine creation in `lru_cache` and clear it from the conftest along with `get_settings`.

```python
# app/db.py
@lru_cache
def get_engine() -> Engine:
    settings = get_settings()
    connect_args = (
        {"check_same_thread": False}
        if settings.database_url.startswith("sqlite")
        else {}
    )
    return create_engine(settings.database_url, echo=settings.debug, connect_args=connect_args)

def init_db() -> None: ...   # uses get_engine()
def get_session() -> Iterator[Session]: ...   # uses get_engine()
```

```python
# tests/conftest.py
@pytest.fixture(autouse=True)
def _isolated_sqlite(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp}/test.db")
        monkeypatch.setenv("SECRET_KEY", "test-secret")
        from app.config import get_settings
        from app.db import get_engine
        get_settings.cache_clear()
        get_engine.cache_clear()
        yield
        get_settings.cache_clear()
        get_engine.cache_clear()
```

## Embedding JSON in an HTML attribute for Alpine

**Symptom.** Page loads but Alpine doesn't initialise. DevTools shows the `x-data` attribute terminating at the first `"` inside the JSON.

**Cause.** Jinja's `|tojson` filter escapes `<`, `>`, `&`, and `'` for HTML safety, but **not `"`**. JSON uses `"` for strings. Inside `x-data="..."` (double-quoted), the first `"` in the JSON ends the attribute.

**Fix.** Use a **single-quoted** attribute and `|tojson`. Make any inline JS strings inside use double quotes so they don't conflict with the outer single quote.

```html
<article x-data='{
  query: {{ (submitted or "") | tojson }},
  users: {{ named_users | tojson }},
  claim(id) {
    htmx.ajax("POST", "/onboard/claim/" + id, { target: "#app", swap: "innerHTML" });
  }
}'>
```

Same rule applies anywhere a `|tojson`-produced value goes into an attribute (`x-show='matches({{ name | tojson }})'`).

## HTMX: updating a second region from one POST

**Symptom.** A single button click must update two different parts of the page that aren't a parent/child of each other.

Two patterns, picked by whether the second region can be in a state you must not destroy:

**OOB swap** — the response body contains BOTH the `hx-target` region's new content AND another fragment marked `hx-swap-oob="true"`; HTMX picks the OOB fragment up by id and swaps it unconditionally. Use when the second region is always safe to replace.

```jinja
<section id="other-region" hx-swap-oob="true">...</section>
```

**Event-driven refresh** — the response carries an `HX-Trigger` header; the second region listens for that event and refetches itself. Use when the second region has a state that must survive (this repo: the order section can be replaced by an inline person-edit form — an OOB swap from a drink save would clobber a half-finished edit; the edit form simply carries no listener, so it's immune, and it re-renders the section fresh on its own save/cancel anyway).

```python
ORDER_REFRESH = {"HX-Trigger": "order-refresh"}

@app.post("/me/drink", response_class=HTMLResponse)
async def save_drink(...):
    upsert_saved_drink(session, user.id, normalized)
    card_html = templates.get_template("_drink_card.html").render(_drink_card_ctx(...))
    return HTMLResponse(card_html, headers=ORDER_REFRESH)
```

```jinja
{# _order_section.html — the event bubbles to body, hence from:body #}
<section id="order-section" hx-get="/order"
         hx-trigger="order-refresh from:body" hx-swap="outerHTML">
```

Costs one extra GET per update; in exchange the refresh is opt-in per state of the target.

## HTMX 2 silently drops 4xx responses

**Symptom.** The server correctly returns a re-rendered form with an error message and status 409/422, tests assert it, yet in the browser clicking Save does nothing — no error appears.

**Cause.** htmx 2's default `responseHandling` only swaps 2xx/3xx responses; 4xx/5xx fire an error event and are discarded. Validation-error fragments never reach the DOM.

**Fix.** Opt 4xx back into swapping via the config meta tag (keep 5xx as errors):

```html
<meta name="htmx-config" content='{"responseHandling":[{"code":"204","swap":false},{"code":"[23]..","swap":true},{"code":"[4]..","swap":true},{"code":"[5]..","swap":false,"error":true}]}' />
```

Note the single-quoted attribute (the JSON needs its double quotes — same rule as the Alpine/tojson section). TestClient can't catch this class of bug: assert the meta tag is present in the base template instead.

## Server-side normalization for forms with hidden axes

**Symptom.** Alpine hides axes that don't apply to the chosen drink, but the form still submits stale values for those hidden inputs.

**Pattern.** Don't try to prevent the submission. Re-apply the per-drink rules server-side and clobber inapplicable fields.

```python
def normalize(base_id: str, form: dict) -> Optional[dict]:
    drink = get_drink(base_id)
    if drink is None: return None
    out = {"base_id": drink.id}
    out["size"] = (
        _pick("size", VALID_SIZES, drink.default_size.value)
        if drink.sized else None
    )
    out["milk"] = (
        _pick("milk", VALID_MILKS, "full_cream")
        if drink.milk_policy == MilkPolicy.REQUIRED else None
    )
    # …same for every axis…
    return out
```

The form is the UX. The rules are the source of truth. They live together in `app/menu.py`.

## Multi-user simulation in tests

```python
def _client(): return TestClient(create_app())

def _user_id_by_name(name):
    from app.db import get_engine
    from app.models import User
    with Session(get_engine()) as s:
        return s.exec(select(User).where(User.display_name == name)).first().id

def test_bob_adds_alice():
    with _client() as alice, _client() as bob:
        alice.get("/"); alice.post("/me/name", data={"display_name": "Alice"})
        alice.post("/me/drink", data={"base_id": "latte", ...})
        alice_id = _user_id_by_name("Alice")

        bob.get("/"); bob.post("/me/name", data={"display_name": "Bob"})
        r = bob.post(f"/order/add/{alice_id}")
        assert "Alice" in r.text
```

Each `TestClient(create_app())` has its own cookie jar — that's how you simulate two browsers. They share the engine (and DB) because the conftest-set `DATABASE_URL` is per-test, not per-client.

## Things this stack doesn't need (don't add)

- **WAL / `busy_timeout` SQLite pragmas.** Two browser tabs hitting the dev server don't conflict. If you see "readonly database" mid-build, the first move is `rm -rf data/ && restart uvicorn` — `--reload` watchers can hold stale connections. Only reach for WAL if you actually observe a collision in test.
- **Alembic migrations.** `SQLModel.metadata.create_all` in lifespan is enough for a demo.
- **A separate `Order` table.** `OrderItem` keyed on `(owner_id, target_user_id)` is sufficient — the set of items for an owner *is* their open order.
- **CSS.** Pico's defaults are fine. Don't add a stylesheet unless the audience specifically asks.
