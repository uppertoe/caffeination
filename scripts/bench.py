"""Route benchmark: seeds a realistic roster, then reports per-route latency
and SQL statement counts.

    .venv/bin/python scripts/bench.py            # default: 150 people
    .venv/bin/python scripts/bench.py --people 400 --iters 50

Runs entirely in-process against a throwaway SQLite file via TestClient, so
the numbers are the app's own cost (routing, queries, template render) with
no network in the way. Statement counts are read back from the Server-Timing
header the app puts on every response, so this doubles as a check that the
production measurement is wired up.
"""

from __future__ import annotations

import argparse
import os
import random
import re
import statistics
import tempfile
import time

# Must run before app imports read settings.
_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/bench.db"
os.environ["SECRET_KEY"] = "bench-secret"
os.environ.setdefault("DEBUG", "false")

from fastapi.testclient import TestClient  # noqa: E402

from app.db import get_engine, init_db  # noqa: E402
from app.main import create_app  # noqa: E402

_QUERIES_RE = re.compile(r'db;desc="(\d+) queries"')


def _queries(response) -> int:
    m = _QUERIES_RE.search(response.headers.get("server-timing", ""))
    return int(m.group(1)) if m else -1


DRINKS = [
    dict(base_id="latte", size="regular", milk="oat"),
    dict(base_id="flat_white", size="small", milk="full_cream"),
    dict(base_id="cappuccino", size="large", milk="skim"),
    dict(base_id="long_black", size="regular"),
    dict(base_id="espresso", strength="double"),
    dict(base_id="mocha", size="regular", milk="soy", sweetener="one_sugar"),
    dict(base_id="latte", size="regular", milk="almond", notes="extra hot"),
]


def seed(people: int, inactive_share: float = 0.3) -> tuple[TestClient, list[str]]:
    """Create `people` roster users with drinks (some inactive), plus the
    benchmarked owner 'Bench' with a handful of others already in the order."""
    from datetime import datetime, timedelta, timezone

    from sqlmodel import Session, select

    from app.models import User

    rng = random.Random(42)
    ids: list[str] = []
    for i in range(people):
        c = TestClient(create_app())
        c.get("/")
        c.post("/me/name", data={"display_name": f"Person {i:04d} {rng.choice('ABCDEFGH')}"})
        c.post("/me/drink", data=rng.choice(DRINKS))

    with Session(get_engine()) as s:
        users = s.exec(select(User).where(User.display_name.is_not(None))).all()
        ids = [u.id for u in users]
        stale = datetime.now(timezone.utc) - timedelta(days=200)
        for u in rng.sample(users, int(len(users) * inactive_share)):
            u.last_active_at = stale
            u.created_at = stale
            s.add(u)
        s.commit()

    owner = TestClient(create_app())
    owner.get("/")
    owner.post("/me/name", data={"display_name": "Bench"})
    owner.post("/me/drink", data=DRINKS[0])
    for uid in ids[:8]:
        owner.post(f"/order/add/{uid}")
    return owner, ids


def bench(owner: TestClient, ids: list[str], iters: int) -> list[tuple[str, list[float], int]]:
    cases = []
    spare = ids[8:]  # not in the order; safe to add/remove per iteration

    def _case(name, fn):
        samples, q = [], 0
        for _ in range(iters):
            t0 = time.perf_counter()
            r = fn()
            samples.append((time.perf_counter() - t0) * 1000)
            assert r.status_code < 400, (name, r.status_code, r.text[:200])
            q = _queries(r)
        cases.append((name, samples, q))

    _case("GET /", lambda: owner.get("/"))
    _case("GET /order", lambda: owner.get("/order"))
    _case("GET /me/drink/edit", lambda: owner.get("/me/drink/edit"))
    _case("POST /me/drink", lambda: owner.post("/me/drink", data=DRINKS[1]))

    def add_remove():
        uid = spare[0]
        owner.post(f"/order/add/{uid}")
        return owner.post(f"/order/remove/{uid}")

    _case("POST /order/add", lambda: owner.post(f"/order/add/{spare[1]}"))
    owner.post(f"/order/remove/{spare[1]}")
    _case("POST /order/remove (after add)", add_remove)

    n = {"i": 0}

    def create_one_off():
        n["i"] += 1
        return owner.post(
            "/people",
            data={"display_name": f"Guest {n['i']}", "base_id": "latte", "one_off": "1"},
        )

    _case("POST /people (one-off)", create_one_off)
    owner.post("/order/clear")
    for uid in ids[:8]:
        owner.post(f"/order/add/{uid}")

    anon = TestClient(create_app())
    _case("GET / (onboarding)", lambda: anon.get("/"))
    return cases


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--people", type=int, default=150)
    ap.add_argument("--iters", type=int, default=30)
    args = ap.parse_args()

    init_db()  # lifespan doesn't run outside `with TestClient(...)`
    owner, ids = seed(args.people)

    cases = bench(owner, ids, args.iters)
    print(f"\n{args.people} roster people, 8 in the order, {args.iters} iterations each\n")
    print(f"{'route':34} {'p50 ms':>8} {'p95 ms':>8} {'queries':>8}")
    for name, samples, q in cases:
        samples.sort()
        p50 = statistics.median(samples)
        p95 = samples[min(len(samples) - 1, int(len(samples) * 0.95))]
        print(f"{name:34} {p50:8.1f} {p95:8.1f} {q:8d}")


if __name__ == "__main__":
    main()
