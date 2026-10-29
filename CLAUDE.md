# CLAUDE.md

Self-hosted job-listing monitor that runs as a single Docker container on TrueNAS SCALE 25.10.

- **Start here:** `docs/DEVELOPMENT_PLAN.md` has the full spec, the architecture, and phased tasks with acceptance criteria.
- Implement one phase at a time. Lint, type checks, and tests must pass before each commit.

## Status
- Phase 0 (scaffold) is done. Next is Phase 1 (database and config).

## Commands
```bash
uv sync                                  # install app + dev deps into .venv
uv run ruff check . && uv run ruff format --check .
uv run pyright
uv run pytest --cov
DATA_DIR=./data uv run jobmonitor serve  # run without Docker on :8080
docker compose -f compose.dev.yaml up --build   # run the hardened container on :8080
```

`[tool.uv] exclude-newer` in `pyproject.toml` keeps every resolved package at least two weeks old.
Move that date forward (and re-run `uv lock`) when updating dependencies.

### Building in a Claude cloud sandbox
Docker Hub is rate-limited and Debian apt mirrors are blocked there. Start `dockerd`, then build with
`docker build --network host --build-arg BASE_REGISTRY=mirror.gcr.io/library --build-arg WITH_BROWSER=false -t jobmonitor:dev .`.
GitHub Actions uses the defaults.

## Decisions log
- **Dependencies are added per phase**, not all up front. For example, Playwright, SQLAlchemy and Argon2 arrive in the phases that use them. The Dockerfile's Chromium step does nothing until `playwright` is installed.
- **uv is installed in the build stage with `pip install uv==<pinned>`**, instead of `COPY --from=ghcr.io/astral-sh/uv`. It is just as reproducible and works where GHCR blob downloads are blocked.
- **No `tini` / apt step in the runtime image.** The slim base already has `ca-certificates` and `tzdata`. Docker's built-in init (`init: true` in both compose files, and `--init` in CI) reaps child processes. Phase 3 will need apt anyway for `playwright install --with-deps`.
- **`BASE_REGISTRY` build arg** (default `docker.io/library`) allows using a registry mirror.
- **Starlette TestClient's httpx deprecation warning is ignored** in pytest config, because pytest treats warnings as errors. The replacement package it names hasn't been vetted yet. Revisit this when upgrading Starlette.
- **Pyright** runs `strict` on `app/` and `standard` on `tests/`.
