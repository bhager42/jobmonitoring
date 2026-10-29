# CLAUDE.md

Self-hosted job-listing monitor that runs as a single Docker container on TrueNAS SCALE 25.10.

- **Start here:** `docs/DEVELOPMENT_PLAN.md` has the full spec, the architecture, and phased tasks with acceptance criteria.
- Implement one phase at a time. Lint, type checks, and tests must pass before each commit.

## Status
- Phases 0–2 are done (scaffold; database and config; authentication and security baseline).
  Next is Phase 3 (scraping engine).

## Commands
```bash
uv sync                                  # install app + dev deps into .venv
uv run ruff check . && uv run ruff format --check .
uv run pyright
uv run pytest --cov                       # fails below 85% coverage
DATA_DIR=./data uv run jobmonitor migrate  # apply migrations (serve also does this)
DATA_DIR=./data uv run jobmonitor serve  # run without Docker on :8080
echo 'new password' | DATA_DIR=./data uv run jobmonitor reset-password --password-stdin
DATA_DIR=./data uv run alembic revision --autogenerate -m "describe change"  # new migration
docker compose -f compose.dev.yaml up --build   # run the hardened container on :8080
scripts/container-smoke-test.sh jobmonitor:dev  # what CI runs against the built image
```

`[tool.uv] exclude-newer` in `pyproject.toml` keeps every resolved package at least two weeks old.
Move that date forward (and re-run `uv lock`) when updating dependencies.

### Building in a Claude cloud sandbox
Docker Hub is rate-limited and Debian apt mirrors are blocked there. Start `dockerd`, then build with
`docker build --network host --build-arg BASE_REGISTRY=mirror.gcr.io/library --build-arg WITH_BROWSER=false -t jobmonitor:dev .`.
If uv then fails with `invalid peer certificate: UnknownIssuer`, the build container needs the
sandbox proxy's CA. Build from a scratch copy of the Dockerfile that adds
`COPY --from=ccr ca-bundle.crt /tmp/ca.crt` and `ENV SSL_CERT_FILE=/tmp/ca.crt PIP_CERT=/tmp/ca.crt`
at the start of the build stage, and pass `--build-context ccr=/root/.ccr`. Never commit that change.
GitHub Actions uses the defaults.

## Auth and security conventions
- **Default deny.** `AuthMiddleware` (`app/auth/middleware.py`) requires a session for every path
  except `PUBLIC_PATHS` and `/static/`. New routes are protected automatically;
  `test_every_route_requires_login_unless_public` fails if one isn't.
- **CSRF.** Unsafe methods need the per-session token: send it in the `X-CSRF-Token` header (htmx gets
  it from `hx-headers` on `<body>`) or as a `csrf_token` form field. Every form needs
  `<input type="hidden" name="csrf_token" value="{{ csrf_token }}">`. Cross-site requests are rejected
  first, using `Sec-Fetch-Site`, then `Origin`.
- **CSP forbids inline scripts and styles**, including `style=""` attributes. Put CSS in
  `app/web/static/css/app.css` and JS in static files. A test fails if a page contains them.
- Colors are `light-dark()` tokens in `:root`. Use the tokens, never raw colors, so dark mode keeps working.
- Templates get `authenticated`, `csrf_token`, and `app_version` from `app/web/templating.py`.

## Database conventions
- Models are in `app/db/models.py`. After changing them, autogenerate a migration, review it, and run
  `ruff format`. `tests/integration/test_migrations.py::test_models_match_migrations` fails if the
  models and migrations drift apart.
- All datetimes are timezone-aware. `UTCDateTime` stores them as naive UTC and rejects naive input.
  Use `app.db.models.utcnow()`.
- `upgrade_to_head` takes a `VACUUM INTO` backup to `/data/backups/pre-migrate-*.db` before changing
  an existing database. It refuses to start on a database migrated by a newer image.

## Decisions log
- **Dependencies are added per phase**, not all up front. For example, Playwright, SQLAlchemy and Argon2 arrive in the phases that use them. The Dockerfile's Chromium step does nothing until `playwright` is installed.
- **uv is installed in the build stage with `pip install uv==<pinned>`**, instead of `COPY --from=ghcr.io/astral-sh/uv`. It is just as reproducible and works where GHCR blob downloads are blocked.
- **No `tini` / apt step in the runtime image.** The slim base already has `ca-certificates` and `tzdata`. Docker's built-in init (`init: true` in both compose files, and `--init` in CI) reaps child processes. Phase 3 will need apt anyway for `playwright install --with-deps`.
- **`BASE_REGISTRY` build arg** (default `docker.io/library`) allows using a registry mirror.
- **Starlette TestClient's httpx deprecation warning is ignored** in pytest config, because pytest treats warnings as errors. The replacement package it names hasn't been vetted yet. Revisit this when upgrading Starlette.
- **Pyright** runs `strict` on `app/` and `standard` on `tests/`.
- **`check_runs.status` also allows `'running'`** (plan §5 lists only finished states). A run row is
  written when the check starts, which is how "Check now" can tell that a check is already running.
- **Extra index on `matches.monitor_id`** for per-monitor match lists and the cascade. The `matches(seen, matched_at)` index is
  ascending; SQLite reads it in reverse for `ORDER BY matched_at DESC`.
- **All §8 environment variables are defined in `Settings` now**, though most are used by later phases.
- **Migrations run synchronously** (stdlib sqlite3) via a programmatic Alembic config, so the image
  needs no `alembic.ini`. The root `alembic.ini` is only for developer commands.
- **Ruff `DTZ` rules are enabled** to catch naive datetimes.
- **The CSRF token is derived, not stored**: HMAC-SHA256(secret_key, session id). It is unique per
  session and changes when the session does, with no extra column.
- **CSRF also uses Fetch Metadata** (`Sec-Fetch-Site`, falling back to `Origin`) on every unsafe
  request, including `/login` and `/setup`, which have no session yet.
- **Auth middleware is raw ASGI.** Starlette's `BaseHTTPMiddleware` can't read a form and still pass
  the body to the route. The middleware buffers up to 1 MB of form body (larger forms get a 413) and
  replays it to the route.
- **Lockout**: 5 failures from one IP in the last 15 minutes, counting only failures since that IP's
  last success. Wrong setup tokens and wrong current passwords on Settings count too.
- **Change password is in Phase 2** (plan §7 lists it under Settings). It signs out every session,
  then starts a fresh one for the current browser.
- **Setup mode re-checks the database on each request**, so a password set by
  `jobmonitor reset-password` closes `/setup` on a running app.
- **Static assets get `Cache-Control: no-cache`** (ETag revalidation) until Phase 5 adds hashed
  filenames for long-lived caching. Everything else gets `no-store`.
