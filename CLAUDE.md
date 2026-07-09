# CLAUDE.md

Project-specific guidance for Claude Code working in this repo.

## What this is

`coffee-rch` is a small web app where office colleagues record a "usual"
coffee order, then anyone can assemble a group order by adding people from
the roster. The output is a till-ready summary (e.g. "2x small lattes, 1x
flat white oat, 1x double espresso"). The repo doubles as a live demo of
LLM-agent-driven development.

## Stack decisions (fixed — don't relitigate)

- FastAPI, Jinja2 server-rendered HTML, HTMX for partials, Alpine.js for
  local UI state, PicoCSS for styling.
- SQLModel + SQLite. One file, mounted volume in compose. No Postgres.
- Cookie-backed identity (`itsdangerous`-signed opaque id). No passwords,
  no email, no OAuth.
- Drinks menu is **hardcoded** in Python (enums / dataclasses), not in DB.
  Adding a new drink is a code change. Orders reference drinks by stable
  string ids so old orders survive menu edits.
- Tests: pytest + FastAPI's `TestClient`. CI gates the image build.
- Docker image is built and pushed to GHCR by GitHub Actions on `main`.

## How to run

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
DEBUG=true uvicorn app.main:app --reload   # http://localhost:8000
pytest                                     # tests
docker compose up --build                  # containerised
# DEBUG=true opts into the dev SECRET_KEY; without it (or a real SECRET_KEY)
# create_app() refuses to start on the forgeable default.
```

## Conventions

- **App factory.** `app.main.create_app()` returns a fresh `FastAPI` instance.
  Tests build their own with a temp DB; the module-level `app = create_app()`
  exists for `uvicorn app.main:app`.
- **Settings are env-driven.** Everything that varies between dev/prod
  belongs in `app/config.py`. Read it via `get_settings()`. The cache is
  cleared in tests so monkeypatched env vars take effect.
- **One file per concept** under `app/` until something grows past ~200
  lines, then split.
- **Templates** live under `app/templates/`. `base.html` owns the layout
  (Pico + HTMX + Alpine CDN tags). Pages extend it. HTMX partials are
  template fragments returned directly, not full pages.
- **HTMX vs Alpine split.** Server-authoritative state (saved drinks, the
  order list) → HTMX request, server re-renders the relevant fragment.
  Ephemeral UI state (which tab is active, "is this checkbox disabled
  because base=espresso") → Alpine `x-data` on the form. Don't reach for
  Alpine to model anything that has to round-trip the DB.
- **Drink rules live with the drink definitions**, not scattered through
  templates. The constraint "espresso has no size" is a property of the
  drink, queried by the form. See the `coffee-taxonomy` skill.
- **No backwards-compat hacks during the live build.** The schema and
  routes will change. Migrations are not needed yet — `init_db()` just
  calls `create_all`. If we keep going past the demo we'll add Alembic.

## What's in the scaffold vs. what we build live

**Scaffold (this commit):**
- Health endpoint, placeholder index, cookie identity, DB init, Dockerfile,
  compose, CI to GHCR, one smoke test.

**Live build, roughly in order:**
1. `User` model + first-visit name capture (one-field form, HTMX swap).
2. Drink taxonomy: enums for base drink, milk, size, sweetener, temp;
   per-drink rule table (size disabled for espresso/piccolo/macchiato,
   shots only meaningful for espresso-family, milk hidden for long
   black / americano, etc.).
3. `SavedDrink` model + a drink-builder form (HTMX form with Alpine
   driving the disabled/visible states from the chosen base).
4. Roster page listing every user + their saved drink.
5. `Order` / `OrderItem` models: each user can have one open order; add
   other users' saved drinks to it; the till summary collapses duplicates
   (`2x small latte`) and lists modifiers.
6. "Copy for the till" button — plain-text summary in a single textarea.

## Skills

- `.claude/skills/coffee-taxonomy/` — Melbourne drink rules, sizes, shot
  semantics, milk options, naming conventions, and the constraint matrix
  for the drink-builder UX.
- `.claude/skills/live-build-playbook/` — the phase plan for going from the
  scaffold to a working group-order app, including the up-front decisions
  to confirm, what to defer, and the three pre-flight scaffold fixes.
- `.claude/skills/stack-patterns/` — concrete patterns for cookie-via-middleware,
  lazy SQLite engine, JSON-in-attribute quoting, OOB swaps, server-side
  normalization, and multi-user TestClient tests.

If the user asks for the live build, lean on **live-build-playbook**; if
you're writing new HTMX/Alpine code or a new model/route, **stack-patterns**
has the recipes that avoid the time-sinks the first build hit.

## See also

- `LIVE_BUILD_PROMPT.md` — the prompt that, given this scaffold, reproduces
  the build the playbook describes. Use it as a starting point if you're
  invoking Claude Code fresh from a clone.

## Things to push back on

- **Don't add Postgres** until we actually need it. SQLite on a mounted
  volume is the design.
- **Don't add auth.** Cookie identity is enough.
- **Don't introduce a SPA.** HTMX + Alpine is the design.
- **Don't add Alembic** during the live build unless we ship.
- **Don't generalise the drinks menu into a CMS.** Hardcoded Python is the
  design — the rules are gnarly enough that data-driven config gets ugly.
