# CLAUDE.md

Self-hosted job-listing monitor that runs as a single Docker container on TrueNAS SCALE 25.10.

- **Start here:** `docs/DEVELOPMENT_PLAN.md` has the full spec, the architecture, and phased tasks with acceptance criteria.
- Implement one phase at a time. Lint, type checks, and tests must pass before each commit.

## Status
- Phase 0 (scaffold) and Phase 1 (database and config) are done. Next is Phase 2 (authentication and security baseline).

## Commands
```bash
uv sync                                  # install app + dev deps into .venv
uv run ruff check . && uv run ruff format --check .
uv run pyright
uv run pytest --cov                       # fails below 85% coverage
DATA_DIR=./data uv run jobmonitor migrate  # apply migrations (serve also does this)
DATA_DIR=./data uv run jobmonitor serve  # run without Docker on :8080
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
