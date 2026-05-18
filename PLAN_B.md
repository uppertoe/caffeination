# Plan B — ship the working build instead of rebuilding live

If the live demo runs out of time, derails, or you'd rather skip the build and just show the audience a finished product: the `live-build` branch is already a working MVP. Merge it to `main`, push, and the CI workflow builds a fresh GHCR image.

## When to switch

- You're 10+ minutes into the build and haven't passed phase 1 → switch.
- A test you didn't write yourself is failing and you can't see why in ~3 minutes → switch.
- An audience question takes the build down a rabbit hole → switch.
- The wifi flakes during a `pip install` → switch.

Plan B is the conservative move. It's not a failure mode; it's the right call any time the audience would be better served by working software than by watching debugging.

## The merge (verified clean as of `070699b`)

```bash
git checkout main
git pull --ff-only
git merge --no-ff live-build -m "Merge live-build: working MVP"
git push origin main
```

`--no-ff` preserves the phase commit history (`Phase 1`, `Phase 2`, …) instead of fast-forwarding past it. The audience sees the build's narrative.

CI on `main` runs the test suite (35 tests, ~1s) then pushes `ghcr.io/uppertoe/caffeination:latest` and `:sha-<short>`.

## What the audience sees

After the merge lands and CI is green:

```bash
docker run --rm -p 8000:8000 \
  -e SECRET_KEY="$(openssl rand -base64 48)" \
  -e DATABASE_URL="sqlite:////data/coffee.db" \
  -v coffee-data:/data \
  ghcr.io/uppertoe/caffeination:latest
```

Open in two browsers — one creates "Alice" with an oat latte, the other hits the page, sees Alice on the onboarding list with her drink, claims her identity OR creates a new one, builds a drink, and adds Alice to the order.

## Why this is pre-validated

`PLAN_B.md` ships at the same commit as the playbook. Before publishing this commit, the merge was dry-run in a git worktree against `live-build`, with `pytest` passing 35/35 on the merged tree. The tip of `live-build` referenced here is `648ad58` (Ignore SQLite WAL/SHM sidecar files); update this section if you rerun the dry-run from a newer tip.

## Recovery if you tried Plan A and want to fall back mid-stream

If you're partway through the build on a fresh branch (not `live-build`) and want to switch:

```bash
git stash -u                 # park anything in-flight
git checkout main
git merge --no-ff live-build -m "Merge live-build: working MVP"
git push origin main
```

The audience sees a working app within the time it takes CI to run (~90s).
