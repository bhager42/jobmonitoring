# Job Monitor

A self-hosted, password-protected web app that watches company careers pages and tells you when a new
job posting matches your criteria. It runs as a single Docker container on TrueNAS SCALE 25.10 and
keeps its data in an embedded SQLite database.

> **Status:** early development (Phase 0 scaffold). The full spec is in
> [`docs/DEVELOPMENT_PLAN.md`](docs/DEVELOPMENT_PLAN.md).

## Run locally

```bash
docker compose -f compose.dev.yaml up --build
```

Then open <http://localhost:8080/healthz>.

## Develop without Docker

```bash
uv sync
```

```bash
DATA_DIR=./data uv run jobmonitor serve
```

TrueNAS install instructions will be added in Phase 7.
