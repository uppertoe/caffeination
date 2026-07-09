# coffee-rch

Group coffee orders for the office. A small FastAPI + HTMX + Alpine + PicoCSS
app for assembling till-ready coffee orders from a roster of people.

This repo is the live-build companion for a demo of using LLM agents to build
a project. The current state is the scaffold: an empty FastAPI app, a health
endpoint, a placeholder index page, and the surrounding plumbing.

## Stack

- **FastAPI** + **Jinja2** templates (server-rendered)
- **HTMX** for partials, **Alpine.js** for local UI state, **PicoCSS** for styling
- **SQLModel** on **SQLite** (single file, mounted volume in compose)
- Cookie-backed identity (signed via `itsdangerous`) — no passwords
- **pytest** + **httpx** via FastAPI's `TestClient`
- **Docker** + **docker-compose** for local; GitHub Actions builds to **GHCR**

## Run it locally

### Option A — virtualenv

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
DEBUG=true uvicorn app.main:app --reload
```

(`DEBUG=true` opts into the known dev `SECRET_KEY`; without it the app
refuses to start on the forgeable default. Set a real `SECRET_KEY` instead
if you prefer.)

Then open http://localhost:8000.

### Option B — docker compose

```bash
docker compose up --build
```

## Tests

```bash
pytest
```

## Deploy

CI builds and pushes the image to GHCR on every branch push:

- `main` → `:latest`, `:main`, `:sha-<short>`
- any other branch (e.g. `live-build`) → `:<branch>`, `:sha-<short>`

Pull and run on any Docker host:

```bash
docker run --rm -p 8000:8000 \
  -e SECRET_KEY=<a-real-secret> \
  -e DATABASE_URL=sqlite:////data/coffee.db \
  -v coffee-data:/data \
  ghcr.io/uppertoe/caffeination:latest          # or :live-build for the MVP build
```

## Where things live

```
app/
  main.py         FastAPI app factory + routes
  config.py       Pydantic settings (env-driven)
  db.py           SQLModel engine + init_db()
  identity.py     Signed-cookie user id
  templates/      Jinja2 templates
  static/         Static assets (vendor CSS/JS later if needed)
tests/            pytest + TestClient
.claude/skills/   Project-specific skills for Claude Code
```

See [`CLAUDE.md`](./CLAUDE.md) for conventions and the live-build plan.

## Running this as a live demo

- [`LIVE_BUILD_PROMPT.md`](./LIVE_BUILD_PROMPT.md) — the prompt to paste into a fresh Claude Code session to reproduce the build, with demo cadence + recovery notes.
- [`PLAN_B.md`](./PLAN_B.md) — the merge-and-ship fallback if the live build runs over.
- `live-build` branch — the finished MVP, with 35 tests passing. Pre-validated as a clean merge into `main`.
