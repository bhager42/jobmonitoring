# Job Monitor — Software Development Plan

> **Audience:** Claude (or any developer) implementing this project from an empty repository.
> Work through the phases in order. Each phase ends with acceptance criteria that must pass
> before moving on. Pin exact dependency versions at implementation time and confirm they are
> current. Do not use any version that is less than two weeks old.

---

## 1. Product summary

A self-hosted, single-user web app that runs as **one Docker container** on **TrueNAS SCALE 25.10
("Goldeye")**. The user:

1. Logs in with a password.
2. Adds a **monitor**: a company careers page URL, the search criteria (keywords, location, and so on),
   and a check interval in hours.
3. The app fetches that page every *N* hours, pulls out the job postings, and records any that are
   **new** and **match the criteria**.
4. The user sees matches on a dashboard. Optional notifications (webhook, ntfy, or email) can be
   added later.

All state lives in an **embedded SQLite database** inside the container's mounted `/data` volume.
There are no external services: no Postgres, no Redis, and no third-party auth.

### Non-goals (v1)
- Multiple users or roles. There is one password-protected account.
- Applying to jobs, or getting past logins or CAPTCHAs on career sites.
- Mobile apps. The web UI is responsive and installable as a PWA, and that is enough.

---

## 2. Technology decisions

| Concern | Choice | Rationale |
|---|---|---|
| Language/runtime | **Python 3.14** (fall back to 3.13 if a dependency lacks 3.14 wheels) | Mature scraping ecosystem, simple single-process app |
| Package manager | **uv** with `pyproject.toml` + `uv.lock` | Fast, reproducible builds |
| Web framework | **FastAPI** + **Uvicorn** | Async, typed, small |
| Templates | **Jinja2** server-rendered HTML | No JS build step; fast, accessible |
| Interactivity | **htmx 2.x**, vendored into `static/` (no CDN) | Progressive enhancement, self-contained |
| ORM / migrations | **SQLAlchemy 2.x** (async, `aiosqlite`) + **Alembic** | Typed models, safe schema upgrades between image versions |
| Database | **SQLite** (WAL mode) at `/data/jobmonitor.db` | Self-contained, zero-ops, easy to back up |
| HTTP client | **httpx** (async, HTTP/2) | Timeouts, retries, proxies |
| HTML parsing | **selectolax** (fast) + **lxml** fallback | CSS-selector extraction |
| JS-rendered sites | **Playwright (Chromium headless)**, used only when needed | Workday and similar sites need JS |
| Scheduler | In-process asyncio loop driven by `next_check_at` in the DB | Survives restarts; no extra dependency |
| Password hashing | **argon2-cffi** (Argon2id) | Current OWASP recommendation |
| Notifications (phase 6) | **Apprise** | One library covers ntfy, Discord, email, Pushover, webhooks, and more |
| Lint/format/types | **ruff**, **pyright** (strict on `app/`) | |
| Tests | **pytest**, **pytest-asyncio**, **respx** (HTTP mocks), **Playwright** (E2E) | |
| CI/CD | GitHub Actions → **GHCR** image `ghcr.io/bhager42/jobmonitoring` | TrueNAS pulls from a registry |

**Image size note:** Chromium adds about 400 MB. Build it with a build arg `WITH_BROWSER=true` (the
default) and also publish a `-lite` tag built without it. The app must detect at runtime that the
browser is missing and turn off the "Render with browser" option in the UI.

---

## 3. "2026 web standards" checklist

The UI must meet all of these. Use only features that are **Baseline Widely Available** as of 2026,
with no polyfills or build step.

**HTML and accessibility**
- Semantic landmarks (`header`, `nav`, `main`, `footer`), one `h1` per page, and a logical heading order.
- **WCAG 2.2 AA**: 4.5:1 contrast, visible `:focus-visible` rings, target size ≥ 24×24 px, no keyboard
  traps, a skip link, `aria-live="polite"` for htmx-swapped status regions, and labelled form controls.
- Native `<dialog>` for confirmations (delete monitor) and the **Popover API** (`popover` attribute) for menus.
- Native form validation (`required`, `type="url"`, `min`/`max`, `pattern`), with server-side validation as the authority.
- `<meta name="viewport" content="width=device-width, initial-scale=1">`, `lang="en"`, and `<meta name="color-scheme" content="light dark">`.

**CSS** (one hand-written stylesheet; no framework)
- `@layer reset, base, components, utilities;`
- Native **CSS nesting**, **custom properties** for design tokens, and **`light-dark()`** for theming.
- **Container queries** for card layouts, `:has()` for state styling, and logical properties (`margin-inline`, and so on).
- `clamp()` for fluid type, `text-wrap: balance` on headings, and `prefers-reduced-motion` respected.
- **View Transitions API** (`@view-transition { navigation: auto; }`) for page-to-page navigation, as progressive enhancement.
- Mobile-first: works at 320 px width with no horizontal scroll.

**Performance**
- No render-blocking third-party resources. All assets are self-hosted.
- Long-cache static assets with content-hashed filenames, and `Cache-Control: no-store` on authenticated HTML.
- Brotli/gzip compression (Starlette `GZipMiddleware` is enough).
- Targets: Lighthouse ≥ 95 on Performance, Accessibility, Best Practices.

**PWA**
- `manifest.webmanifest` with icons (SVG + 192/512 PNG maskable) and `display: standalone`.
- A minimal service worker that caches **static assets only**, never authenticated pages or API responses.

**Security headers** (middleware, on every response)
- `Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'`. No inline scripts or styles; configure htmx with `htmx.config.includeIndicatorStyles = false` so it works under this CSP.
- `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`,
  `Permissions-Policy: camera=(), microphone=(), geolocation=()`,
  `Cross-Origin-Opener-Policy: same-origin`, `Cross-Origin-Resource-Policy: same-origin`.
- `Strict-Transport-Security` **only** when `SECURE_COOKIES=true` (the app is behind HTTPS).

---

## 4. Architecture

```
┌──────────────────────── container (UID 568 "apps") ───────────────────────┐
│                                                                           │
│  Uvicorn ── FastAPI app                                                   │
│     ├── auth middleware (session cookie → sessions table)                 │
│     ├── web routes (Jinja2 + htmx)       /, /monitors, /matches, /settings│
│     ├── JSON API (same auth)             /api/v1/...                      │
│     ├── /healthz (no auth)                                                │
│     └── lifespan: starts Scheduler + Backup task                          │
│                                                                           │
│  Scheduler (asyncio task)                                                 │
│     every 30 s: SELECT monitors WHERE enabled AND next_check_at <= now    │
│     → bounded worker pool (MAX_CONCURRENT_CHECKS, default 3)              │
│     → CheckRunner.run(monitor)                                            │
│                                                                           │
│  CheckRunner                                                              │
│     Fetcher (pick adapter) → list[RawPosting]                             │
│     → normalize → fingerprint → diff vs. stored postings                  │
│     → Matcher(criteria) → insert matches → Notifier (phase 6)             │
│                                                                           │
│  SQLite (WAL) ── /data/jobmonitor.db      Backups ── /data/backups/       │
└───────────────────────────────────────────────────────────────────────────┘
```

**One process, one worker.** Run Uvicorn with `--workers 1` so there is exactly one scheduler.
Concurrency comes from asyncio.

### 4.1 Fetcher adapters (the core of reliability)

Many careers pages are front-ends for an Applicant Tracking System (ATS) that has a public JSON
endpoint. Hitting that endpoint is far more reliable than scraping HTML. Adapters are tried in this
order:

| # | Adapter | Detection (from user URL or page HTML) | Source |
|---|---|---|---|
| 1 | Greenhouse | `boards.greenhouse.io/{token}`, `job-boards.greenhouse.io/{token}`, or embed script `boards.greenhouse.io/embed/job_board?for={token}` | `GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true` |
| 2 | Lever | `jobs.lever.co/{company}` | `GET https://api.lever.co/v0/postings/{company}?mode=json` |
| 3 | Ashby | `jobs.ashbyhq.com/{org}` | `GET https://api.ashbyhq.com/posting-api/job-board/{org}?includeCompensation=true` |
| 4 | SmartRecruiters | `jobs.smartrecruiters.com/{company}` or `careers.smartrecruiters.com/{company}` | `GET https://api.smartrecruiters.com/v1/companies/{company}/postings` (paginate) |
| 5 | Workday | `*.myworkdayjobs.com/{site}` or `*.wd{N}.myworkdayjobs.com/...` | `POST https://{host}/wday/cxs/{tenant}/{site}/jobs` JSON `{"limit":20,"offset":N,"searchText":""}` (paginate) |
| 6 | JSON-LD | Page contains `<script type="application/ld+json">` with `@type: JobPosting` | Parse schema.org JobPosting objects |
| 7 | CSS selector | User supplies an "item selector" (plus optional title, link, and location sub-selectors) in Advanced settings | selectolax on the fetched HTML |
| 8 | Heuristic links | Fallback: anchors whose `href` or text look like job postings (`/job/`, `/jobs/`, `/careers/`, `/positions/`, `/opening`, `?gh_jid=`, and so on) | Title = link text, URL = absolute href |

Each adapter can run in **static** mode (httpx) or, for adapters 6–8, **rendered** mode
(Playwright: wait for `networkidle` or a user-supplied selector, 30 s timeout). Rendered mode is used
when the user selects "Render with browser" or when static mode yields 0 postings and
`auto_render=true`.

Adapter interface:

```python
class FetchAdapter(Protocol):
    name: str
    async def detect(self, url: str, html: str | None) -> AdapterConfig | None: ...
    async def fetch(self, cfg: AdapterConfig, ctx: FetchContext) -> list[RawPosting]: ...

@dataclass
class RawPosting:
    external_id: str | None     # ATS id when available
    title: str
    url: str                    # absolute
    location: str | None
    department: str | None
    employment_type: str | None
    remote: bool | None
    posted_at: datetime | None
    description_text: str | None  # plain text, truncated to 20k chars
```

When a monitor is created, the app runs **auto-detection** once, stores the chosen adapter and its
config on the monitor, and shows the user a **preview** of the postings it extracted before they save.

### 4.2 Polite fetching rules (must implement)
- A `User-Agent` that identifies the app: `JobMonitor/{version} (+self-hosted; personal job alerts)`.
- Respect `robots.txt` by default (cached for 24 h). There is a per-monitor override checkbox labelled with a warning.
- Per-host concurrency of 1, and at least 2 s between requests to the same host (this covers pagination).
- Minimum check interval of **1 hour**. Add ±10% random jitter to every `next_check_at`.
- Timeouts: 10 s connect, 30 s read. Retry up to 3 times with exponential backoff on 429/5xx/network errors, honoring `Retry-After`.
- Cap pagination at 50 pages or 2,000 postings per check.
- Cap responses at 10 MB.
- **SSRF guard:** reject URLs whose resolved IPs are private, loopback, or link-local, unless `ALLOW_PRIVATE_URLS=true`. Allow only `http` and `https` schemes.

### 4.3 Posting identity and change detection
- `fingerprint = sha256(adapter + ":" + (external_id or canonical_url(url)) )`, where
  `canonical_url` lower-cases the host and drops the fragment and tracking params (`utm_*`, `gh_src`, `source`, `lever-source`, and so on).
- A posting is **new** the first time its fingerprint is seen for that monitor.
- Track `first_seen_at`, `last_seen_at`, and `is_active`. A posting is marked inactive when it is missing from 2 consecutive successful checks.
- **The first check of a new monitor is the baseline.** It stores all postings but does *not*
  raise "new match" alerts. Instead it shows "N existing postings match your criteria" with a link.
  A checkbox lets the user include existing matches as alerts if they want.

### 4.4 Matching criteria

Stored as JSON on the monitor and validated with a Pydantic model:

```json
{
  "keywords_any":   ["python", "backend"],
  "keywords_all":   [],
  "keywords_none":  ["senior staff", "intern"],
  "title_only":     true,
  "title_regex":    null,
  "locations_any":  ["remote", "denver", "colorado"],
  "remote_only":    false,
  "departments_any": [],
  "employment_types_any": [],
  "posted_within_days": null
}
```

Rules:
- Matching is case-insensitive, Unicode-normalized (NFKC + casefold), and on **whole words / phrases** (regex `\b...\b` around escaped terms).
- `title_only=false` also searches the description text.
- Empty lists are ignored. All non-empty conditions must pass (AND across fields, OR within `_any`).
- `title_regex` is compiled with a 100 ms timeout using the `regex` module, so a catastrophic pattern cannot hang the worker.
- The matcher is a **pure function** `matches(posting, criteria) -> MatchResult(matched: bool, reasons: list[str])`. The reasons ("title contains 'python'") appear in the UI.

---

## 5. Data model (SQLite)

Turn on these pragmas for every connection: `journal_mode=WAL`, `foreign_keys=ON`, `busy_timeout=5000`, `synchronous=NORMAL`.

```
settings          (key TEXT PK, value TEXT)               -- password_hash, secret_key, app prefs
sessions          (id TEXT PK  -- sha256 of token, created_at, last_seen_at, expires_at,
                   user_agent, ip)
login_attempts    (id PK, ip, attempted_at, success BOOL)
monitors          (id PK, name, url, adapter, adapter_config JSON, render_mode TEXT
                     CHECK IN ('static','browser','auto'),
                   criteria JSON, interval_hours INT CHECK >=1 AND <=720,
                   enabled BOOL, respect_robots BOOL, notify BOOL,
                   last_check_at, next_check_at, last_status TEXT, last_error TEXT,
                   consecutive_failures INT, baseline_done BOOL,
                   created_at, updated_at)
postings          (id PK, monitor_id FK→monitors ON DELETE CASCADE, fingerprint,
                   external_id, title, url, location, department, employment_type,
                   remote, posted_at, description_text,
                   first_seen_at, last_seen_at, missing_count INT, is_active BOOL,
                   UNIQUE(monitor_id, fingerprint))
matches           (id PK, monitor_id FK, posting_id FK UNIQUE, reasons JSON,
                   matched_at, seen BOOL DEFAULT 0, dismissed BOOL DEFAULT 0,
                   notified_at)
check_runs        (id PK, monitor_id FK, started_at, finished_at, status TEXT
                     CHECK IN ('ok','error','blocked','skipped'),
                   adapter_used, postings_found INT, new_postings INT,
                   new_matches INT, error TEXT, duration_ms INT)
notification_targets (id PK, name, apprise_url_encrypted, enabled)   -- phase 6
```

Indexes: `monitors(enabled, next_check_at)`, `postings(monitor_id, is_active)`,
`matches(seen, matched_at DESC)`, `check_runs(monitor_id, started_at DESC)`.

Retention: delete `check_runs` older than 90 days, and inactive postings with no match older than 180 days. This runs daily.

---

## 6. Authentication (single password)

- **First run:** if `settings.password_hash` is missing:
  - If `APP_PASSWORD` or `APP_PASSWORD_FILE` is set, hash it with Argon2id, store the hash, and never log it.
  - Otherwise, generate a random one-time **setup token**, print it to the container log
    (`Setup token: XXXX — open http://<host>:<port>/setup`), and serve `/setup`, which requires that token to
    set the password (minimum 12 characters). The setup route disappears once a password exists.
- **Login:** `POST /login` checks the password with Argon2id. On success it creates a random 32-byte session token.
  The database stores only its SHA-256 hash. The cookie is `jm_session`, with `HttpOnly`, `SameSite=Strict`, `Path=/`,
  and `Secure` when `SECURE_COOKIES=true`. When secure, it uses the name `__Host-jm_session`.
  Sessions use a 7-day sliding expiry with a 30-day absolute expiry.
- **Rate limiting:** after 5 failed attempts per IP in 15 minutes, lock that IP out for 15 minutes. Add a constant
  ~300 ms delay to every failed login.
- **CSRF:** a synchronizer token per session is embedded in a `<meta>` tag. htmx sends it as the `X-CSRF-Token` header
  (`hx-headers` on `<body>`), and plain forms send it as a hidden field. Validate it on every unsafe method. Also check the `Origin`/`Sec-Fetch-Site` headers.
- **Change password** on the Settings page. This rotates all sessions.
- **Recovery:** running `docker exec <ctr> jobmonitor reset-password` (a CLI entry point) prompts for a new password.
  Document this in the README.
- `secret_key` is generated on first boot and stored in `settings`. It is used for CSRF and for encrypting notification URLs (Fernet).
- `TRUST_PROXY=true` makes the app honor `X-Forwarded-For`/`X-Forwarded-Proto` (via Uvicorn `--proxy-headers --forwarded-allow-ips`), for use behind a reverse proxy.

---

## 7. UI pages

| Route | Contents |
|---|---|
| `/login`, `/setup` | Minimal centered form |
| `/` Dashboard | Unseen matches (newest first) as cards: title, company/monitor, location, the reasons it matched, first seen, "Open posting ↗", "Mark seen", and "Dismiss". Summary counts. Monitors in an error state are flagged. |
| `/monitors` | Table/cards: name, URL host, adapter badge, interval, last check (relative time plus status icon), next check, active postings, match count, enabled toggle, "Check now". |
| `/monitors/new` and `/monitors/{id}/edit` | Step 1: URL → **Detect** (htmx), which shows the adapter found and a preview of the first 20 postings. Step 2: criteria form, with a **live match preview** (htmx, debounced 400 ms) showing which preview postings would match and why. Step 3: name, interval (hours), and Advanced (render mode, CSS selectors, respect robots). |
| `/monitors/{id}` | Detail view: criteria summary, recent check runs (status, duration, counts, error), current active postings with match highlighting, and "Check now". |
| `/matches` | All matches with filters (monitor, seen/unseen, dismissed) and pagination. |
| `/settings` | Change password, global defaults (interval, max concurrency, timezone display), backup ("Download backup now" streams a `VACUUM INTO` copy), notifications (phase 6), and an About section (version, browser available yes/no). |

"Check now" enqueues the check immediately and refuses if one is already running for that monitor.
The UI polls the status region with `hx-trigger="every 3s"` only while a check is running.

---

## 8. Configuration (environment variables)

| Var | Default | Notes |
|---|---|---|
| `PORT` | `8080` | Port inside the container |
| `DATA_DIR` | `/data` | DB, backups, browser profile cache |
| `TZ` | `UTC` | Set to the local zone in TrueNAS |
| `APP_PASSWORD` / `APP_PASSWORD_FILE` | — | Optional initial password |
| `SECURE_COOKIES` | `false` | Set `true` behind HTTPS |
| `TRUST_PROXY` | `false` | Honor `X-Forwarded-*` |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | Proxy IPs to trust |
| `MAX_CONCURRENT_CHECKS` | `3` | |
| `DEFAULT_INTERVAL_HOURS` | `12` | |
| `ALLOW_PRIVATE_URLS` | `false` | SSRF guard override |
| `BACKUP_KEEP` | `7` | Number of daily backups to keep |
| `LOG_LEVEL` | `info` | Structured JSON logs to stdout |

Use `pydantic-settings` to load these. Fail fast on invalid values.

---

## 9. Repository layout

```
.
├── CLAUDE.md                      # pointers for future Claude sessions
├── README.md                      # user docs incl. TrueNAS install
├── docs/
│   ├── DEVELOPMENT_PLAN.md        # this file
│   └── truenas-app.yaml           # compose YAML for "Install via YAML"
├── pyproject.toml / uv.lock
├── Dockerfile
├── .dockerignore
├── compose.dev.yaml               # local dev
├── alembic.ini
├── app/
│   ├── __init__.py                # __version__
│   ├── main.py                    # FastAPI factory, lifespan, middleware
│   ├── config.py                  # pydantic-settings
│   ├── cli.py                     # `jobmonitor` CLI: serve, reset-password, migrate, backup
│   ├── db/
│   │   ├── engine.py              # async engine + pragmas
│   │   ├── models.py
│   │   └── migrations/            # alembic env + versions
│   ├── auth/                      # passwords, sessions, csrf, rate limit, deps
│   ├── security/                  # headers middleware, ssrf guard
│   ├── scraping/
│   │   ├── base.py                # RawPosting, FetchAdapter, FetchContext
│   │   ├── http.py                # polite httpx client, robots cache, host limiter
│   │   ├── browser.py             # Playwright lifecycle (lazy, shared, optional)
│   │   ├── detect.py              # auto-detection orchestration
│   │   └── adapters/              # greenhouse, lever, ashby, smartrecruiters,
│   │                              # workday, jsonld, css_selector, heuristic
│   ├── matching/
│   │   ├── criteria.py            # pydantic Criteria model
│   │   └── matcher.py             # pure matching function
│   ├── services/
│   │   ├── checks.py              # CheckRunner
│   │   ├── scheduler.py
│   │   ├── backup.py
│   │   └── notify.py              # phase 6
│   ├── web/
│   │   ├── routes/                # pages + htmx partials
│   │   ├── api/                   # /api/v1 JSON
│   │   ├── templates/             # base.html, partials/, pages/
│   │   └── static/                # css/app.css, js/htmx.min.js, js/app.js, icons, manifest, sw.js
└── tests/
    ├── fixtures/                  # saved ATS JSON + HTML pages
    ├── unit/                      # matcher, fingerprint, url canon, adapters, ssrf
    ├── integration/               # app + temp SQLite, auth flows, check runner w/ respx
    └── e2e/                       # Playwright UI smoke tests
```

---

## 10. Container build

**Dockerfile** (multi-stage):

```dockerfile
# syntax=docker/dockerfile:1
ARG PYTHON_VERSION=3.14
FROM python:${PYTHON_VERSION}-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:<pinned> /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY app ./app
RUN uv sync --frozen --no-dev

FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime
ARG WITH_BROWSER=true
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright DATA_DIR=/data PORT=8080
RUN apt-get update && apt-get install -y --no-install-recommends tini ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /app /app
RUN if [ "$WITH_BROWSER" = "true" ]; then \
      playwright install --with-deps chromium && rm -rf /var/lib/apt/lists/*; fi
# TrueNAS default apps user/group is 568
RUN groupadd -g 568 apps && useradd -u 568 -g 568 -M -s /usr/sbin/nologin apps \
    && mkdir -p /data && chown 568:568 /data
USER 568:568
WORKDIR /app
EXPOSE 8080
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8080\")}/healthz',timeout=4)"
ENTRYPOINT ["/usr/bin/tini","--"]
CMD ["jobmonitor","serve"]
```

The `jobmonitor serve` command:
1. ensures `DATA_DIR` is writable (and exits with a clear message about dataset permissions/UID 568 if it is not),
2. runs `alembic upgrade head`,
3. does first-run password handling,
4. execs Uvicorn with `--workers 1` and the proxy-header flags when `TRUST_PROXY` is set.

Chromium launch args: `--no-sandbox` is **not** needed with a non-root user plus Docker's default seccomp policy. If launch fails, fall back to `--disable-dev-shm-usage` and document `shm_size`.
Launch the browser lazily and close it after 5 minutes idle to save RAM.

**CI (GitHub Actions, `.github/workflows/ci.yml`):**
- On PRs: `uv sync`, `ruff check`, `ruff format --check`, `pyright`, `pytest` (unit + integration), and a Docker build (no push).
- On push to `main` and on tags `v*`: build `linux/amd64` (TrueNAS SCALE is x86-64 only), run E2E tests against the built image, and push to GHCR as `:latest`, `:vX.Y.Z`, `:sha-<short>`, plus `-lite` variants. Generate an SBOM and provenance with `docker/build-push-action`.
- Dependabot for `uv`, `github-actions`, and `docker`.

---

## 11. TrueNAS SCALE 25.10 deployment

Since 24.10, TrueNAS SCALE apps run on **Docker Compose**. Deploy as a **Custom App via YAML**.

**Prerequisites (document in README):**
1. *Datasets* → create `pool/apps/jobmonitor` (preset: **Apps**).
2. Edit its permissions so user/group **apps (568)** has Modify/Full Control.
3. *Apps* → pick a pool if prompted.
4. If the GHCR package is private, add a registry credential (*Apps → Configuration → Manage Container Image Registries*).
   Making the package public is simpler.

**`docs/truenas-app.yaml`** (paste into *Apps → Discover Apps → ⋮ → Install via YAML*):

```yaml
services:
  jobmonitor:
    image: ghcr.io/bhager42/jobmonitoring:latest
    container_name: jobmonitor
    restart: unless-stopped
    init: true
    user: "568:568"
    ports:
      - "8686:8080"            # host:container — change 8686 if taken
    environment:
      TZ: America/Denver       # set to your timezone
      SECURE_COOKIES: "false"  # "true" if behind an HTTPS reverse proxy
      # APP_PASSWORD: "change-me"   # optional; otherwise use the setup token in logs
    volumes:
      - /mnt/pool/apps/jobmonitor:/data   # adjust pool name
    shm_size: "512m"           # Chromium
    security_opt:
      - no-new-privileges:true
    cap_drop: [ALL]
    read_only: true
    tmpfs:
      - /tmp:size=256m,mode=1777
    deploy:
      resources:
        limits:
          memory: 1536M
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=4)"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 20s
```

**Acceptance test on TrueNAS:** the app installs, the status shows *Running* and healthy, the web UI loads at
`http://<truenas-ip>:8686`, the setup token appears in *App → Logs*, data survives *Stop/Start* and an image
update, and `/mnt/pool/apps/jobmonitor/jobmonitor.db` exists and is owned by 568.

**Updating:** push a new image, then use *Apps → jobmonitor → Update* (or Edit → Save to re-pull `:latest`).
Migrations run automatically on start. Before migrating, the app takes an automatic `VACUUM INTO` backup to `/data/backups/pre-migrate-<ts>.db`.

**Remote access** (README guidance only): use the TrueNAS Tailscale app or a reverse proxy (Caddy / Nginx
Proxy Manager) with HTTPS. Then set `SECURE_COOKIES=true` and `TRUST_PROXY=true`. Do not port-forward plain HTTP.

---

## 12. Implementation phases

Each phase is a separate commit (or PR) with passing CI.

### Phase 0 — Scaffold
- `pyproject.toml` with dependencies, `uv.lock`, ruff/pyright config, `app/` skeleton, `/healthz`, `CLAUDE.md`, `README.md` stub.
- Dockerfile, `.dockerignore`, `compose.dev.yaml`, CI workflow (lint + test + build).
- **Done when:** `docker compose -f compose.dev.yaml up` serves `/healthz` → `{"status":"ok","version":...}` as UID 568, and CI is green.

### Phase 1 — Database and config
- Settings, the async engine with pragmas, models, Alembic initial migration, and `jobmonitor migrate`.
- **Done when:** a fresh `/data` gets a DB created at head, running it again is a no-op, and the unit tests for the pragmas pass.

### Phase 2 — Authentication and security baseline
- Setup token flow, login/logout, sessions, CSRF, rate limiting, security-headers middleware, `reset-password` CLI, base layout template, and CSS design tokens.
- **Done when:** the integration tests cover setup → login → protected page → logout, CSRF rejection, lockout after 5 failures,
  every response carrying the CSP and other headers, unauthenticated requests redirecting to `/login` (HTML) or getting 401 (API), and session cookie flags that are correct in both `SECURE_COOKIES` modes.

### Phase 3 — Scraping engine
- Polite HTTP client, robots cache, SSRF guard, all 8 adapters, auto-detection, URL canonicalization, fingerprinting, and the optional Playwright browser.
- Save realistic fixtures under `tests/fixtures/` (trimmed real API responses and HTML pages) and test every adapter offline with respx.
- **Done when:** each adapter has detection and parsing tests, the SSRF tests block `127.0.0.1`, `10.x`, `169.254.169.254`, `[::1]`, and DNS-rebinding style hostnames,
  and a manual `jobmonitor probe <url>` CLI prints the detected adapter and its postings.

### Phase 4 — Matching, check runner, scheduler
- The Criteria model, the matcher (with property-based tests via **hypothesis** for normalization and word boundaries), CheckRunner (diff, baseline, inactive tracking, check_runs logging), the scheduler with jitter and concurrency limits, backoff for failing monitors (after 5 consecutive failures, double the interval up to 7 days and flag it in the UI), retention cleanup, and daily backups.
- **Done when:** the integration test runs CheckRunner against mocked sites: the baseline creates no alerts, adding a posting to the mock creates exactly one match, removing it twice marks it inactive, and restarting the app does not re-alert. A scheduler test using a fake clock checks the timing.

### Phase 5 — Web UI
- All pages from §7, htmx partials, the live preview, the PWA manifest and service worker, and accessibility passes.
- **Done when:** the Playwright E2E tests cover setup, creating a monitor against a local fixture server (with `ALLOW_PRIVATE_URLS=true` in tests), seeing the preview, saving, "Check now", a match appearing, and marking it seen. An axe-core scan (`@axe-core/playwright` or `axe-playwright-python`) reports 0 serious or critical violations. The layout works at 320 px and 1440 px, in light and dark.

### Phase 6 — Notifications (optional but recommended)
- Apprise targets (stored encrypted), a per-monitor notify toggle, one digest per check run (not one per posting), a "Send test" button, and `notified_at` tracking.
- **Done when:** a mocked Apprise test shows one notification per run with new matches, and none on baseline runs.

### Phase 7 — Packaging and release
- Multi-tag GHCR publishing, `docs/truenas-app.yaml`, a full README (install, first login, adding a monitor, troubleshooting: permissions/UID 568, port conflicts, sites that need browser mode, password reset, backup/restore), and `CHANGELOG.md`.
- **Done when:** the TrueNAS acceptance test in §11 passes on a 25.10 system, and a `v1.0.0` tag is published.

---

## 13. Testing strategy summary

| Layer | Tooling | Must cover |
|---|---|---|
| Unit | pytest, hypothesis | matcher, criteria validation, canonical URL, fingerprint, each adapter parser, SSRF guard, password/session utils |
| Integration | pytest + httpx `ASGITransport` + temp SQLite + respx | auth flows, CSRF, headers, CRUD routes, CheckRunner end-to-end, migrations up from empty |
| E2E | Playwright (Python) against the built Docker image | the golden-path user journey and accessibility scan |
| Container | CI job | image runs as 568, read-only root FS works, healthcheck turns healthy, `/data` persistence across restart |

Coverage target: at least 85% of lines in `app/` (excluding `browser.py`), enforced in CI.

---

## 14. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Career sites change markup | Prefer ATS JSON adapters. Per-monitor preview. A clear error state plus "0 postings found" warning after a previously non-zero result. |
| Bot protection (Cloudflare, etc.) | Report the status as `blocked` with an explanation. Do not try to evade it. Suggest the company's ATS URL instead. |
| Chromium memory use on NAS | Lazy launch, idle shutdown, one shared browser, at most 2 concurrent pages, and a memory limit in compose. |
| SQLite locking | WAL, a single process, short transactions, and `busy_timeout`. |
| Lost password | `reset-password` CLI via TrueNAS app shell. |
| Data loss | Daily `VACUUM INTO` backups plus pre-migration backups. Users can also snapshot the dataset in TrueNAS. |

---

## 15. Instructions for Claude when executing this plan

1. Read this file and `CLAUDE.md` first. Keep `CLAUDE.md` updated with commands (`uv run pytest`, `uv run ruff check`, `docker compose -f compose.dev.yaml up --build`) and any decisions that change this plan.
2. Do one phase at a time. Run lint, type checks, and tests locally before each commit. Do not move on with red tests.
3. Pin every dependency and image (`uv.lock`, Docker image digests or exact tags, GitHub Action SHAs).
4. Never commit secrets, real `.db` files, or `.env` files. `.gitignore` must cover `data/`, `*.db*`, `.env*`, and `.venv/`.
5. If a choice here proves unworkable (for example, an ATS endpoint has changed), adapt, write the reason down under a **Decisions log** heading in `CLAUDE.md`, and add a fixture test for the new behavior.
