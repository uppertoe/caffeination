# Deploying caffeine@RCH

This folder mirrors the per-app layout of the
[anaes-data-lab/rch-server-image](https://github.com/anaes-data-lab/rch-server-image)
VPS, where each app lives under `apps/<name>/` with its own compose file, a
Caddy routing snippet, and an `.env.example`.

## Add it to the server

1. Copy this folder into the server repo as `apps/caffeine-rch/`:
   - `docker-compose.yml`
   - `caffeine-rch.caddy`
   - `.env.example`
2. Create the real env file and set a secret:
   ```bash
   cp apps/caffeine-rch/.env.example apps/caffeine-rch/.env
   # SECRET_KEY=$(openssl rand -hex 32)
   ```
3. Add one include line to the server's top-level `docker-compose.yml`:
   ```yaml
   include:
     - apps/caffeine-rch/docker-compose.yml
   ```
4. Deploy as usual. The Caddy snippet is picked up automatically, so the app
   comes up at `https://caffeine.<DOMAIN>`.

## Image

This repo's CI builds and pushes `ghcr.io/uppertoe/caffeination` on every push
to `main` (tags: `latest`, `main`, `sha-<short>`). The compose file tracks
`:latest`; in the server repo, pin to a digest — Renovate's `pinDigests` keeps
it current.

**The GHCR package must be public** for the VPS to pull without credentials.
GHCR packages start *private* even for a public repo, so after the first CI
publish: GitHub → Packages → `caffeination` → Package settings → Change
visibility → Public. If you'd rather keep it private, run
`docker login ghcr.io` on the VPS with a `read:packages` token first.

## Data & backups

All state is a single SQLite file in the `caffeine_data` volume at
`/data/caffeine.db`. To include it in the server's backups, add a service entry
under `backup/services/` (see the server repo's `ansible/backup.yml`).

## Auth

The app deliberately has no login — identity is a signed cookie, so colleagues
just type their name. To gate it behind the SSO gateway instead, add
`import protected` in `caffeine-rch.caddy` (see `apps/auth/auth.caddy`).

## Notes

- The container runs read-only with `cap_drop: ALL` and `no-new-privileges`;
  it only writes to the `caffeine_data` volume and `/tmp`.
- The image runs `uvicorn` with `--proxy-headers --forwarded-allow-ips=*` so it
  trusts Caddy's `X-Forwarded-Proto` and marks the identity cookie `Secure`
  behind TLS.
