# CLAUDE.md

Self-hosted job-listing monitor that runs as a single Docker container on TrueNAS SCALE 25.10.

- **Start here:** `docs/DEVELOPMENT_PLAN.md` has the full spec, the architecture, and phased tasks with acceptance criteria.
- Implement one phase at a time. Lint, type checks, and tests must pass before each commit.

## Status
- Phases 0–4 are done (scaffold; database and config; authentication and security baseline;
  scraping engine; matching, check runner and scheduler). Next is Phase 5 (web UI).

## Commands
```bash
uv sync --all-extras                     # app + browser extra + dev deps (plain `uv sync` removes Playwright)
uv run ruff check . && uv run ruff format --check .
uv run pyright
uv run pytest --cov                       # fails below 85% coverage
DATA_DIR=./data uv run jobmonitor migrate  # apply migrations (serve also does this)
DATA_DIR=./data uv run jobmonitor serve  # run without Docker on :8080
echo 'new password' | DATA_DIR=./data uv run jobmonitor reset-password --password-stdin
ALLOW_PRIVATE_URLS=true uv run jobmonitor probe <url> [--render static|browser] [--item-selector ...]
DATA_DIR=./data uv run alembic revision --autogenerate -m "describe change"  # new migration
docker compose -f compose.dev.yaml up --build   # run the hardened container on :8080
scripts/container-smoke-test.sh jobmonitor:dev  # what CI runs against the built image
```

`[tool.uv] exclude-newer` in `pyproject.toml` keeps every resolved package at least two weeks old.
Move that date forward (and re-run `uv lock`) when updating dependencies.

## Vulnerability handling
- CI audits every locked Python package with pip-audit (OSV) and scans the built image with Grype
  (high or critical, fixable). CI also runs daily, so a new advisory fails the build without a code change.
- **Security fixes skip the two-week rule.** Add the package to `exclude-newer-package` in
  `pyproject.toml` with a comment naming the advisory. Then run
  `uv lock --upgrade-package <name>` and `uv sync`. Remove the entry once `exclude-newer` passes its date.
- Dependabot's 14-day cooldown applies to version updates only; security updates are not delayed.
  They need Dependabot alerts and security updates turned on in the repository settings.
- To audit locally: `uv export --locked --all-groups --no-emit-project --no-hashes -o /tmp/req.txt`,
  then `uvx pip-audit==2.10.1 -r /tmp/req.txt --no-deps --disable-pip`.

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

## Scraping conventions
- Code lives in `app/scraping/`. Adapters are in `adapters/`, and the registry
  (`adapters/__init__.py`) sets detection order. **API adapters** (Greenhouse, Lever, Ashby,
  SmartRecruiters, Workday) implement `detect` and `fetch`. **Page adapters** (JSON-LD, CSS selector,
  heuristic) implement `detect` and `parse(html)`. `fetch.py` loads pages statically or in the browser.
- **All outbound HTTP goes through `PoliteClient`** (`http.py`). Never create a bare httpx client:
  the client is what enforces the SSRF guard, robots.txt, per-host spacing, retries and size limits.
- **SSRF**: `validate_url` runs before every request and redirect hop. `GuardedNetworkBackend`
  re-resolves and checks at connect time, then connects to the checked IP. In the browser, every
  request goes through `BrowserManager._guard`.
- Use `parse_datetime`, `get_str`, `dicts` and similar helpers from `adapters/_util.py` for JSON.
  They keep Pyright strict happy without `ignore` comments.
- Tests run offline: respx for HTTP, `tests/scraping_helpers.fake_resolver` for DNS, fixtures in
  `tests/fixtures/`. **The fixtures are hand-written from the providers' documented response
  shapes, using fictional companies.** They were not captured live (the sandbox network blocks
  these hosts). If a live site disagrees, capture and anonymize a real response, replace the
  fixture, and fix the adapter.
- Browser tests use a real Chromium and skip if none can start. Locally, set `CHROMIUM_EXECUTABLE`
  (in the cloud sandbox: `/opt/pw-browsers/chromium-*/chrome-linux64/chrome`).

## Checks and scheduling conventions
- `app/matching/`: `Criteria` (Pydantic, stored as `monitors.criteria`) and the pure
  `matches(PostingView, Criteria, now=...)`. Validate user input with `Criteria.model_validate` and
  show its errors. The runner treats saved criteria that no longer validate as a failed check.
- `app/services/checks.py` `CheckRunner.run(monitor_id)` does one check. It fetches outside any DB
  transaction, then applies results in one short transaction. Fingerprints include the adapter
  name. **When the UI changes a monitor's URL or adapter, reset `baseline_done=False`**, otherwise
  every posting looks new and raises an alert.
- `app/services/scheduler.py`: `app.state.scheduler`. For "Check now", call `request_check(id)`
  (False means already queued or running). After creating or editing a monitor, call `wake()`
  (or set `next_check_at=None` to make it due at once). `is_busy(id)` tells the UI a check is in
  progress. Use `checks.is_backing_off(monitor)` to flag monitors in the UI.
- Daily maintenance (`services/maintenance.py`) runs from the scheduler tick: it prunes 90-day-old
  check runs and 180-day-old unmatched inactive postings, then backs up to `/data/backups/daily-*.db`.
- Tests that build the app use `create_app(..., start_scheduler=False)` (see
  `tests/integration/conftest.py`) unless they test the background loop on purpose.

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
- **robots.txt applies to pages, not to ATS JSON APIs.** Those are documented public APIs meant
  for programmatic use. Missing robots.txt (4xx) allows everything. A 5xx or unreachable
  robots.txt disallows for 1 hour (RFC 9309).
- **No lxml.** selectolax 1.0's Lexbor parser handles all HTML. Its old `selectolax.parser`
  module raises ImportError in 1.0, so always import from `selectolax.lexbor`.
- **Playwright is an optional `browser` extra.** The `-lite` image (`WITH_BROWSER=false`) leaves it
  out. The full image installs only Chromium's headless shell (`--only-shell`).
- **Chromium always gets `--disable-dev-shm-usage`** and flags that turn off its own background
  traffic. Without them it contacted google.com during a test run.
- **Browser SSRF protection is best-effort.** Chromium resolves DNS itself, so the request guard
  (pre-resolution) can't stop DNS rebinding the way the HTTP client's connect-time check does.
  The final page URL is re-checked too.
- **`OUTBOUND_PROXY` / `OUTBOUND_CA_BUNDLE` / `CHROMIUM_EXECUTABLE`** are advanced settings. With a
  proxy, only the pre-flight DNS check guards private addresses.
- **Workday `postedOn` text becomes an approximate date.** "30+ Days Ago" becomes None.
- **New column `monitors.alert_existing`** (migration `46cba8648788`) backs the plan's "include
  existing matches as alerts" checkbox for the baseline check.
- **Postings with no date pass `posted_within_days`.** Most sites don't publish dates, and the
  posting is new to us.
- **`locations_any: ["remote"]` also matches postings flagged remote**, even if their location
  text doesn't say so.
- **Whole-word matching uses `(?<!\w)term(?!\w)`**, not `\b`, so terms like "c++" and ".net" work.
- **Zero-result safeguard:** if a check finds 0 postings where there used to be some, it records a
  warning (monitor `last_status="warning"`, check run `ok` with a message) and marks nothing
  missing. Failed checks never count toward "missing" either.
- **Backoff:** from the 5th consecutive failure the interval doubles per failure, capped at 7 days
  (or the monitor's own interval if that is longer). Jitter is ±10% on every schedule.
- **Interrupted checks** (still `running` at startup) are closed as errors when the app starts.
- **Static assets get `Cache-Control: no-cache`** (ETag revalidation) until Phase 5 adds hashed
  filenames for long-lived caching. Everything else gets `no-store`.
