# CLAUDE.md

Self-hosted job-listing monitor that runs as a single Docker container on TrueNAS SCALE 25.10.

- **Start here:** `docs/DEVELOPMENT_PLAN.md` has the full spec, the architecture, and phased tasks with acceptance criteria.
- Implement one phase at a time. Lint, type checks, and tests must pass before each commit.

## Status
- Phases 0–7 are implemented, and the version is 1.0.0. Two parts of Phase 7's "done" condition
  need a person: running the §11 acceptance test on a real TrueNAS 25.10 system, and pushing the
  `v1.0.0` tag (which publishes the images). `scripts/truenas-acceptance.sh` runs the same checks
  with Docker Compose and runs in CI.

## Commands
```bash
uv sync --all-extras                     # app + browser extra + dev deps (plain `uv sync` removes Playwright)
uv run ruff check . && uv run ruff format --check .
uv run pyright
uv run pytest --cov                       # fails below 85% coverage; includes the browser E2E test
E2E_SCREENSHOTS=/tmp/shots uv run pytest tests/e2e   # keep screenshots (light/dark, 320/1440 px)
DATA_DIR=./data uv run jobmonitor migrate  # apply migrations (serve also does this)
DATA_DIR=./data uv run jobmonitor serve  # run without Docker on :8080
echo 'new password' | DATA_DIR=./data uv run jobmonitor reset-password --password-stdin
ALLOW_PRIVATE_URLS=true uv run jobmonitor probe <url> [--render static|browser] [--item-selector ...]
DATA_DIR=./data uv run alembic revision --autogenerate -m "describe change"  # new migration
docker compose -f compose.dev.yaml up --build   # run the hardened container on :8080
scripts/container-smoke-test.sh jobmonitor:dev  # what CI runs against the built image
scripts/truenas-acceptance.sh jobmonitor:dev     # docs/truenas-app.yaml via Compose: plan §11 checks
E2E_IMAGE=jobmonitor:dev uv run pytest tests/e2e # the browser journey against an image
```

`[tool.uv] exclude-newer = "14 days"` in `pyproject.toml` keeps every resolved package at least two
weeks old. The window rolls, and `uv.lock` stores the span, so `--locked` doesn't go stale.
Dependabot's cooldown is 15 days so its proposals always fit the window. (A fixed date used to make
every Dependabot `uv` update fail.)

## Releasing
- CI (`.github/workflows/ci.yml`) runs on every push, every PR, daily, and when called by the
  release workflow. For both image variants (full and `-lite`) it runs the smoke test, the TrueNAS
  acceptance script, the browser journey against the image, and Grype.
- `release.yml` runs for tags `vX.Y.Z`, or from the Actions tab (Release > Run workflow) with a
  version. The manual path creates the tag itself, on the commit it tested. Claude cloud sessions
  can't push tags (the git proxy only accepts pushes to the session branch), so they start releases
  this way, with the GitHub tool's `run_workflow`. Both paths check that the version matches
  `app/__init__.py` and has a CHANGELOG entry, call CI, then publish
  `ghcr.io/bhager42/jobmonitoring` with tags `X.Y.Z`, `X.Y`, `X` and `latest` (plus `-lite`
  versions), along with an SBOM and provenance. Pre-release tags (`v1.2.0-rc.1`) get only their exact
  version. Branch pushes never publish.
- To release: bump `app/__init__.py`, date the `CHANGELOG.md` entry, merge, wait for green CI, then
  either push `vX.Y.Z` or run the Release workflow with `X.Y.Z`. The first time, make the GHCR
  package public. The workflow creates the GitHub release with that version's CHANGELOG entry.
- `docs/truenas-app.yaml` is user-facing. Keep it in sync with the README install steps, and keep the
  `image:`, data path and `"30880:8080"` lines exactly as `scripts/truenas-acceptance.sh` expects
  (it substitutes them).

## Vulnerability handling
- CI audits every locked Python package with pip-audit (OSV) and scans the built image with Grype
  (high or critical, fixable). CI also runs daily, so a new advisory fails the build without a code change.
- **Security fixes skip the two-week rule.** Add the package to `exclude-newer-package` in
  `pyproject.toml` with a comment naming the advisory. Then run
  `uv lock --upgrade-package <name>` and `uv sync`. Remove the entry once it is over 14 days old.
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
- Browser tests use a real Chromium and skip if none can start. They prefer a preinstalled
  **headless shell** (`/opt/pw-browsers/chromium_headless_shell-*/*/chrome-headless-shell`), which
  is what the image ships. The full Chromium build makes its own calls to Google services
  (accounts, check-in, updater, google.com) even with `LAUNCH_ARGS`, while the headless shell makes
  none; this was measured with Chromium's net log and the sandbox proxy. `CHROMIUM_EXECUTABLE`
  overrides the choice.

## Web UI conventions
- Server-rendered Jinja2 plus **htmx 2.0.11, vendored** in `app/web/static/js/` (hash-checked by
  `tests/unit/test_vendored.py`; see `VENDORED.md`). Its config lives in a `<meta name="htmx-config">`
  in `base.html`. `historyCacheSize: 0` keeps private pages out of localStorage, and `allowEval: false`
  plus `includeIndicatorStyles: false` keep it within the CSP. A test checks all of these.
- **Every htmx action must also work as a plain form post.** Routes check `is_htmx(request)` and
  return a fragment (`templates/partials/`) or redirect/full page. Forms carry `csrf_token` hidden
  fields; htmx sends the header from `<body hx-headers>`.
- **Static URLs:** always `{{ static('path') }}`. It adds `?v=<content hash>`, which is served with
  `immutable` caching. Never hard-code `/static/...` in templates except in `manifest.webmanifest`.
- `app/web/forms.py` `MonitorForm` is the single parser and validator for create, edit and detect.
  Errors are per field and shown with `aria-invalid`/`aria-describedby` via `_macros.html`.
- Detection previews live in an in-memory `PreviewCache` (30 min, 50 entries). The form carries
  only a token, and saving reuses the detected adapter only if the URL and CSS fields are unchanged.
- `/sw.js` is served from the root (public, also during setup). It caches only versioned
  `/static/` files, never pages.
- `tests/e2e/` runs the real app (`python -m app.cli serve`) against a fake careers site, in
  Chromium. It asserts no serious/critical axe-core violations, no horizontal overflow at 320 and
  1440 px in light and dark, and no console errors. axe-core 4.14.0 is vendored in
  `tests/e2e/vendor/` (tests only, MPL-2.0).
- Inputs have an intrinsic minimum width. Fields set `inline-size: 100%; min-inline-size: 0`, and
  text that can be long (URLs, paths) uses `overflow-wrap: anywhere`, so 320 px phones don't scroll
  sideways.

## Notification conventions
- `app/services/notify.py`. `Notifier.notify_matches` is the runner's `on_new_matches` callback and
  never raises. It sends **one digest per check run** (max 20 items listed), never on a baseline
  check, only if `monitor.notify` is on, to every enabled target. `Match.notified_at` is set only
  after a successful delivery. The last success and failure are kept in the `settings` table
  (`notify_last_sent`, `notify_last_error`) and shown on the Settings page.
- `Sender` is a protocol. `AppriseSender` is the real one (Apprise is sync, so it runs in a thread).
  Tests use `tests/integration/test_notify.FakeSender`. Route tests and the E2E journey also use
  real Apprise against a local `json://` webhook.
- Use `_apprise()` (typed `Any`) instead of importing Apprise directly; its hints are partially
  untyped and fail Pyright strict.
- Notification URLs are encrypted with `app/security/secrets.SecretBox`. **Never render a decrypted
  URL**: show `sender.masked(url)`. Apprise's own logger is set to WARNING because it logs full URLs
  at debug level.

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
- **Global defaults on the Settings page are read-only** (they come from environment variables).
  The plan listed them under Settings, but making them editable would mean a second source of truth.
- **SQLAlchemy 2.1:** `Select` is variadic (`Select[Match]`, not `Select[tuple[Match]]`) and
  `Result.tuples()` is deprecated, so unpack rows directly. `dict(result)` misbehaves because
  results have `.keys()`; use a comprehension.
- **`Match.posting` / `Match.monitor` relationships use `lazy="raise"`.** Load them with
  `joinedload` (`routes/matches.with_details`) so templates never trigger lazy I/O.
- **The notification encryption key is a file, `/data/notify.key` (0600), not derived from
  `secret_key`** (plan §6 suggested deriving it). The secret key lives in the database, so a
  database copy (e.g. a downloaded backup) would otherwise contain both the encrypted URLs and the
  means to decrypt them. If the key file is lost, targets show "saved by another install" and must
  be re-added.
- **Baseline checks never notify, even with `alert_existing`.** Those matches still appear in the
  UI, but a first check can find dozens of them.
- **Notification URLs are not SSRF-filtered.** The admin enters them, and LAN targets (self-hosted
  ntfy, Gotify, Home Assistant) are a normal use. Only careers-page fetching is guarded.
- **Failed notifications are not retried.** The matches stay in the UI, and the failure is shown
  on the Settings page.
- **E2E runs against `jobmonitor serve` by default; with `E2E_IMAGE` it runs against an image**
  (CI does this for both variants, with `--network host` so the container can reach the test site).
- **Host port 30880, not 8686** (plan §11). 8686 is Lidarr's default, a common TrueNAS app.
- **The TrueNAS YAML uses `:latest`** (owner's choice), so it follows every release, including a
  future 2.x. The comment and README point to `:1` for anyone who wants to stay on 1.x.
  `scripts/truenas-acceptance.sh` substitutes the `image: ...:latest` line.
- **Images are published only from version tags.** The plan had `:latest` from every push to main,
  but this repo has no `main`, and an untested branch push shouldn't reach users' NAS boxes.
- **CI runs on pushes to every branch.** Before, it only ran on `main` (which doesn't exist) and on
  PRs, so it had never run.
- **License: AGPL-3.0-or-later** (owner's choice). `LICENSE` is the canonical GNU text, unmodified.
  Section 13 (network use) is met by the footer link to `app.SOURCE_URL` on every page, and
  `test_every_page_offers_the_source_code` guards it. Image labels carry the license too. New
  dependencies must be GPL-compatible (permissive licenses are fine; avoid GPL-2.0-only).
  `regex` is "Apache-2.0 AND CNRI-Python"; it's widely used in GPL projects, but it's flagged here.
- **Static assets get `Cache-Control: no-cache`** (ETag revalidation) until Phase 5 adds hashed
  filenames for long-lived caching. Everything else gets `no-store`.
