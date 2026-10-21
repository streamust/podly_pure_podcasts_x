# Contributor Guide

To run Podly without changing code, follow the
[beginner's guide](how_to_run_beginners.md). This guide covers developing,
deploying behind a proxy, and sending changes.

## Running Podly with Docker

`run_podly_docker.sh` wraps `docker compose` with `compose.yml`. By default it
pulls the published image (`ghcr.io/maroonbrian1928/podly-pure-podcasts:latest`).
To run your own changes, build from your working tree instead:

```bash
./run_podly_docker.sh -d             # pull the published image and start
./run_podly_docker.sh --rebuild -d   # build from your working tree, then start
./run_podly_docker.sh --build        # build only
./run_podly_docker.sh --test-build   # build with no cache
./run_podly_docker.sh -h             # list every option
```
 The app serves the web UI and the API on port 5001, and
Flask serves the built frontend, so there is no separate frontend server.

The script sets `PUID`/`PGID` to your user so files in `src/instance` stay
yours. `CORS_ORIGINS` controls CORS and defaults to any origin. Every other
setting is in [`.env.local.example`](../.env.local.example), and environment
variables override what the web UI stores (see
[How settings are resolved](configuration.md#how-settings-are-resolved)).

## Deploying behind a reverse proxy

Podly builds feed and audio URLs from the request's `Host` header, and trusts
`X-Forwarded-Proto` from the proxy so links use `https`.

### Reverse proxy examples

**Nginx:**

```nginx
server {
    listen 443 ssl;
    server_name your-domain.com;

    location / {
        proxy_pass http://localhost:5001;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

**Traefik** (labels in `compose.yml`), which sends both headers by default:

```yaml
labels:
  - "traefik.enable=true"
  - "traefik.http.routers.podly.rule=Host(`your-domain.com`)"
  - "traefik.http.routers.podly.tls.certresolver=letsencrypt"
  - "traefik.http.services.podly.loadbalancer.server.port=5001"
```

## Authentication

- `REQUIRE_AUTH=true` turns on login. The code default is `false`;
  `.env.local.example` sets it to `true`.
- With auth on, Podly refuses to start without `PODLY_ADMIN_PASSWORD`.
  `PODLY_ADMIN_USERNAME` sets the admin name (default `podly_admin`).
- Set `PODLY_SECRET_KEY` to a long random value. Without it, Podly generates a
  new key on each boot and signs everyone out.
- Podly creates the admin account on first boot with an empty database. To turn
  auth on for an existing install, start from a fresh database.
- After you change the admin password in the Config page, update
  `PODLY_ADMIN_PASSWORD` too, or the next restart fails.
- **Subscribe to Podly RSS** on a feed's page copies a URL with a per-feed
  token, so podcast apps can fetch the feed without your password. Podly
  rate-limits repeated failed attempts.

## FAQ

**What does "whitelisted" mean in the UI?** The episode is eligible for download
and ad removal. New episodes are whitelisted automatically
(`automatically_whitelist_new_episodes`), and a new feed whitelists a limited
number of older episodes
(`number_of_episodes_to_whitelist_from_archive_of_new_feed`). Change both on the
Config page.

## Contributing

### Development setup

Fork the repository, clone your fork, and work on a branch:

```bash
git clone https://github.com/<you>/podly_pure_podcasts_x.git
cd podly_pure_podcasts_x
git checkout -b feature/your-feature-name
```

### Running tests

Install the dev dependencies once, then run the same checks as CI (formatting,
linting, type checks, Python and Rust tests):

```bash
uv sync --extra dev
scripts/ci.sh
```

`scripts/ci.sh` formats and auto-fixes files, so review your working tree after
it runs.

### Database migrations

The database migrates itself on launch. After you change a model, generate a
migration:

```bash
./scripts/create_migration.sh "[change description]"
```

The Rust writer pins one schema revision. A model or migration change must also
update the writer's SQL and `EXPECTED_SCHEMA_REVISION`, and refresh the snapshot
with `uv run python scripts/check_writer_schema.py --update`. CI fails
otherwise.

### Commit messages

Releases come from `semantic-release`, which reads
[Conventional Commits](https://www.conventionalcommits.org/). Include at least
one, such as `feat: add new episode filter` or `fix(api): handle empty feed`,
or the release has nothing to publish.

### Pull requests

Keep each pull request to one feature or fix, so a reviewer can summarize it in
one sentence. Split larger work into a series, and open an issue first if you
want to discuss the direction.

1. Run `scripts/ci.sh` and make sure it passes.
2. Update the docs if behavior changes.
3. Describe the change and link related issues.

### Code style

Ruff formats the code. New code needs type hints and should follow the patterns
around it.
