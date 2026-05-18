---
name: live-build-playbook
description: Use this skill when the user asks you to build out the coffee-rch app on top of the scaffold (or asks for a similar live-build). It lays out the up-front decisions to confirm, the four phases to ship, what to commit between phases, and the gotchas to anticipate so the demo doesn't stall on debugging. Read [[stack-patterns]] alongside for the technical patterns each phase relies on.
---

# live-build-playbook

This is the playbook for taking `coffee-rch` from "scaffold runs, page is blank" to "two browsers can build a group coffee order and copy a till-ready summary". It was distilled from doing the build once already — the failure modes below cost real time the first run and shouldn't cost time the second.

## Confirm decisions before you write code

Ask these as one `AskUserQuestion` block (one question per axis) so the user can answer them in a single round-trip:

1. **Auth model.** Default: signed-cookie identity, no passwords. Skip OAuth/email unless the user pushes for it — it adds 30 minutes of scaffolding for zero demo value.
2. **Order model.** Default: per-user orders (each user assembles their own group order from other users' saved drinks). Reject "shared single round" — the per-user model is what the original prompt actually describes.
3. **Database.** Default: SQLite + SQLModel. Reject Postgres in compose — it adds a moving part with no upside at this scale.
4. **Deploy target.** Default: GHCR image only. Don't wire CD to a host live unless the user asks.
5. **Drinks menu source.** Default: hardcoded Python enums/dataclasses. Resist the urge to make this DB-driven; the constraint rules (espresso has no size, magic is fixed-everything, macchiato has a length axis) are gnarly enough that data-driven config gets ugly fast.
6. **Tests.** Default: pytest + httpx via FastAPI's `TestClient`. They run in <1s and pay for themselves the first time you change a route.
7. **JS layer.** Default: Alpine.js for local UI state, HTMX for server-state. Both via CDN in `base.html`.

If those defaults match the user's intent, the rest of this skill applies as-is.

## Pre-flight fixes to the scaffold

The scaffold ships with three known sharp edges. Address them BEFORE phase 1 so they don't bite mid-demo. Each fix is small; see [[stack-patterns]] for the exact code.

1. **Make `db.engine` lazy.** Replace the module-level `engine = create_engine(...)` with an `@lru_cache` function `get_engine()`. Without this, tests can't swap `DATABASE_URL` — the engine binds at import time and the conftest's monkeypatched env var has no effect.
2. **Move cookie issuance to middleware.** A dependency that sets a cookie on the FastAPI-injected `Response` will see its cookie *silently discarded* if the route returns a `TemplateResponse` (FastAPI uses your returned Response and drops the temporal one). Instead: have the dependency stash a token on `request.state`, and a `BaseHTTPMiddleware` writes the Set-Cookie header on the way out.
3. **Condition the cookie's `Secure` flag on `request.url.scheme`.** `secure=True` blocks the cookie from being sent back over plain http — which is what the test client uses (`http://testserver`) and what local dev uses (`http://localhost`). `secure=not settings.debug` looks pragmatic but creates a "tests pass locally with DEBUG=true, break in CI without it" trap. Just key off the scheme.

Land all three in one "wire identity" commit before phase 1 so the rest of the build is unblocked.

## The four phases

Commit after each, with passing tests. Don't skip the "verify in a real browser via curl" step at the end of each phase — it's where rendering bugs (like JSON inside a double-quoted x-data attribute) show up.

### Phase 1 — User model + name capture
- `User(id, display_name, created_at)` SQLModel table.
- `get_current_user` dependency that resolves the cookie to a `User`, minting one if missing.
- `GET /` renders an onboarding view when `display_name` is None, dashboard otherwise.
- `POST /me/name` accepts a name and returns the dashboard fragment.
- Tests: cold visit shows onboarding; POST /me/name then GET / shows the dashboard with the name.

### Phase 2 — Drink taxonomy + SavedDrink + builder
- `app/menu.py` with enums (Family, MilkPolicy, Size, Milk, Strength, Sweetener, Temp, Length) and a `Drink` dataclass holding the constraint flags. The full menu and constraint matrix live in [[coffee-taxonomy]] — don't redesign it, just transcribe it.
- `SavedDrink` model (one per user; enum values stored as strings).
- `_drink_form.html` rendered with a per-drink `rules` object injected via `{{ rules | tojson }}` into a **single-quoted** `x-data='...'` attribute. Alpine `x-show` drives the visibility of each axis based on the chosen base. Server-side `normalize()` re-applies the same rules on submit so stale fields can't sneak in.
- `format_drink(drink, saved)` writes the till line: omit axes that match the drink's default, lowercase, espresso doubles read as "double", macchiato puts length before the name.
- Tests: unit-test the formatter for the obvious cases (regular latte → "latte", iced soy mocha with notes → "iced soy mocha (...)"); HTTP test the round-trip.

### Phase 3 — Order + roster
- `OrderItem(owner_id, target_user_id)` — composite primary key. No separate `Order` table; an "open order" is just the set of `OrderItem` rows for an owner.
- Roster helper: every named user with a saved drink, minus the owner, minus users already in the order.
- `_order_section.html` rendered with both the order list (owner first if they have a drink) and the roster. Saving a drink hits `POST /me/drink` which returns the drink card *and* the order section with `hx-swap-oob="true"` — that's how the owner's row appears/updates without a page reload.
- Tests: a second TestClient can simulate a second user (engines share via the env-driven URL). Multi-user assertions are the main payoff of `test_order_flow.py`.

### Phase 4 — Till summary + copy
- `till_summary(rows)` groups identical drinks into `Nx <line>` entries. Free-text notes never merge. Group key is `(base_id, size, milk, shots, strength, temp, sweetener, length)`.
- Add a `<textarea readonly>` with the joined lines and a copy button driven by Alpine + `navigator.clipboard.writeText($refs.tillText.value)`.
- Tests: unit-test `till_summary` on the merge/non-merge cases; one HTTP test that the textarea contains the expected lines after a roster add.

## Polish pass: onboarding + roster search

These two improvements were asked for in the first run and are small enough to do as a follow-up commit before deploy:

- **Cookie-less visitors see the existing named users with their drink lines.** Click "That's me" → `POST /onboard/claim/{id}` rebinds the cookie (via `set_identity(request, id)` on the request state) and deletes the empty placeholder row. Type a new name → "Create new" appears below; submit posts to `POST /me/name` like before. Reject duplicate display names case-insensitively.
- **Roster gets a filter input.** Alpine `x-show` filters the rendered list client-side; the data is already in the DOM, so don't round-trip per keystroke.

## Heuristics

- **HTMX for server state, Alpine for local UI state.** "Which roster names match my filter" → Alpine. "Who's in the order" → HTMX.
- **One POST that affects two regions → OOB swap.** Saving a drink updates both the drink card (hx-target) and the order section (out-of-band). Two `templates.get_template(...).render(...)` calls concatenated in an `HTMLResponse` is the simplest way.
- **Trust the server to normalize.** The form renders every axis with `x-show`; hidden axes still submit values. The server clamps/discards on every save against the same rules object. Don't try to mirror visibility in submitted form data.
- **Phase commits are not draft commits.** Each phase commit should leave tests green and the page interactive. If something's half-wired, push the rest into the next phase.

## Things to NOT do during the live build

- Don't introduce Alembic. `SQLModel.metadata.create_all(engine)` in lifespan is fine; we're not shipping the schema anywhere.
- Don't pluralize till lines ("2x lattes" — no). The skill calls for "2x latte" and that's what the audience will recognize.
- Don't add an explicit "close order" or "round complete" concept. Out of scope.
- Don't introduce CSS files. Pico ships with sensible defaults; the demo is about flow, not visual design.
- Don't preemptively optimize SQLite for concurrency. Two users hitting the dev server in tabs works fine without WAL or busy_timeout. Only reach for those if you actually see a write conflict — even then, the first thing to try is restarting the dev server because Uvicorn's `--reload` can leave stale watchers holding open connections.
