# The live-build prompt

Paste the following into a fresh Claude Code session after cloning this repo and running `pip install -e ".[dev]"`. It's the prompt that should reproduce the build documented in `.claude/skills/live-build-playbook/`.

The prompt is calibrated to skip past the things that ate the most time the first run: it confirms decisions upfront, names the skills to lean on, and tells Claude to land the three pre-flight scaffold fixes in one commit before phase 1.

---

## The prompt (copy-paste)

```
We're going to build out the coffee-rch app on top of this scaffold, live,
for an audience. Use the playbook in `.claude/skills/live-build-playbook/`
and the patterns in `.claude/skills/stack-patterns/`. The coffee taxonomy
in `.claude/skills/coffee-taxonomy/` is the source of truth for drinks.

Goal: a working group-order app where users land on the page, see existing
named users + their drinks, claim or create an identity, build a "usual"
drink with the constraint matrix enforced, then assemble a group order by
adding people from a searchable roster. Output is a till-ready textarea
that collapses identical drinks ("2x latte") and a copy button.

Decisions (don't re-ask):
- Identity: signed-cookie, no auth, with a claim-or-create onboarding.
- Order model: per-user (each user assembles their own group order).
- DB: SQLite + SQLModel, single file, lifespan create_all.
- Drinks menu: hardcoded Python enums + dataclass, NOT db-driven.
- JS: HTMX for server state, Alpine for local UI state, via CDN.
- Tests: pytest + TestClient. Phase commits stay green.
- Branch: work on `live-build`. Don't merge to main during the build.

Pre-flight (one commit before phase 1):
1. Make `db.engine` lazy (lru_cache get_engine()).
2. Move cookie issuance to a `BaseHTTPMiddleware` + request.state stash —
   see stack-patterns "Cookies set in a dependency are silently dropped".
3. Set `secure=request.url.scheme == "https"` so the TestClient (and
   localhost http) round-trip cookies. Update conftest to also clear
   get_engine.cache_clear() alongside get_settings.

Phases, each its own commit with passing tests:

  1. User model + first-visit name onboarding (claim existing OR create
     new). Duplicate names rejected case-insensitively. The named-user
     list + drink lines render server-side; an Alpine search filters them
     client-side.

  2. Drink taxonomy + SavedDrink + drink builder. The constraint matrix
     (per-drink sized/milk/shot/length/strength/iced flags) lives in
     `app/menu.py`. Inject it into the form via `{{ rules | tojson }}` in
     a SINGLE-QUOTED `x-data='...'` attribute (see stack-patterns on JSON
     in attributes — this one bit the first build). Server `normalize()`
     clamps stale fields on submit. `format_drink()` writes till lines.

  3. OrderItem(owner_id, target_user_id). Order section combines owner's
     row (if they have a drink), the added users, and the roster with an
     Alpine search filter. Saving a drink returns the card AND the order
     section as an OOB swap so the owner row appears without a reload.

  4. till_summary() groups identical drinks into "Nx <line>" entries;
     notes never merge. Add a readonly <textarea> + Alpine clipboard-copy
     button.

Don't ask which features to add beyond these. Don't add auth, Alembic,
Postgres, SQLite WAL pragmas, or CSS. Don't pluralize till lines. Don't
introduce an Order table. Push back if I ask for any of those.

After phase 4: a smoke test via curl that creates two users, has one add
the other, and dumps the till textarea contents. Then push the branch.

Ready when you are — confirm the plan in one sentence and start with the
pre-flight commit.
```

---

## Notes on using the prompt

- **Set a model.** Opus is the right choice for the live build — it gets the constraint-matrix design right and writes good first-draft tests. Sonnet works but you'll spend more time correcting test scaffolding.
- **Keep `auto-accept edits` on.** The build is mostly mechanical once the plan is set; manually approving every Write tool kills the demo pacing.
- **Decide on demo cadence up front.** "Approve each phase commit" (a natural breakpoint) is better than "approve each file write" (slow) or "approve nothing" (audience can't follow).
- **Have the audience pick a drink.** Phase 2's drink-builder demo is much more memorable if you let someone in the room order. Pick a complex one — short macchiato, magic, or a long-black-with-an-extra-shot.
- **If the demo runs short on time**, skip the claim-or-create polish pass and the roster search — they're labeled "polish" in the playbook and stand alone as a follow-up.

## Recovery moves

- **"Server error" / 500 on first visit**: usually a stale `data/coffee.db` from a prior run. `rm -rf data/` and restart uvicorn.
- **Alpine looks dead, page renders fine**: the `x-data` attribute likely terminates early because of unescaped `"` in embedded JSON. Switch to single-quoted attribute (see stack-patterns).
- **Tests pass locally but a route doesn't see the cookie**: `secure=True` on http. Check the cookie's flags via `curl -c /tmp/jar -b /tmp/jar -v`.
- **"attempt to write a readonly database"** when only one tab is open: see the WAL note in stack-patterns — almost always solved by `rm -rf data/` + restart, not by code changes.
