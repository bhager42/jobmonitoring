# Job Monitor

A self-hosted web app that watches company careers pages and tells you when a new job posting
matches what you're looking for. It runs as a single Docker container on **TrueNAS SCALE 25.10**
(or anywhere Docker runs), keeps everything in a built-in SQLite database, and is protected by a
password.

- Paste a careers page address. Job Monitor works out how to read it: Greenhouse, Lever, Ashby,
  SmartRecruiters and Workday boards through their public feeds, and other sites through job data
  embedded in the page, links that look like postings, or CSS selectors you supply. Pages that build
  their job list with JavaScript are read with a built-in headless browser.
- Choose keywords, words to exclude, locations, remote-only and more. A live preview shows which
  current postings would match, and why.
- Checks run on a schedule you set. The first check only records what's already listed; after that,
  each new matching posting appears on your dashboard. You can also get one notification per check
  through ntfy, Discord, email, Pushover, Home Assistant or
  [any service Apprise supports](https://github.com/caronc/apprise/wiki).

Contents: [Install on TrueNAS](#install-on-truenas-scale-2510) ·
[First sign-in](#first-sign-in) · [Add a monitor](#add-a-monitor) ·
[Notifications](#notifications) · [Updating](#updating) · [Backups and restore](#backups-and-restore) ·
[Remote access](#remote-access) · [Configuration](#configuration) ·
[Troubleshooting](#troubleshooting) · [Development](#development)

## Install on TrueNAS SCALE 25.10

Since 24.10, TrueNAS runs apps with Docker Compose, so Job Monitor installs as a **custom app from
YAML**. Menu names below are from 25.10 and may differ slightly in other versions.

### 1. Create a dataset for its data

1. Go to **Datasets**, select the pool or parent dataset you use for apps, and click
   **Add Dataset**.
2. Name it `jobmonitor` and set **Dataset Preset** to **Apps**. Save.
3. Select the new dataset and, under **Permissions**, click **Edit**. Make sure the **apps** user
   (UID 568) has **Modify** (or Full Control). Add an entry for it if it's missing, then save.

Note the dataset's path, for example `/mnt/tank/apps/jobmonitor`.

### 2. Install the app

1. Go to **Apps**. If asked, choose the pool for apps.
2. Open **Discover Apps**, click the three-dot menu at the top right, and choose
   **Install via YAML**.
3. Name the app `jobmonitor` and paste the contents of
   [`docs/truenas-app.yaml`](docs/truenas-app.yaml). Then change the two lines marked `CHANGE`:
   - `TZ` to your [time zone](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones),
     e.g. `Europe/London`;
   - `/mnt/tank/apps/jobmonitor` to your dataset's path from step 1.
4. Click **Save**. TrueNAS pulls the image and starts the app. Wait until it shows **Running**.

The app listens on port **30880**: open `http://<your-truenas-address>:30880`.

The file uses image tag `:1`, which follows the newest 1.x release. There's also a smaller
`:1-lite` image without the headless browser. It works for most sites, but can't read careers pages
that only show jobs after running JavaScript.

## First sign-in

On first start there's no password yet, so the app prints a one-time **setup token** to its log.
This proves you're the person who installed it.

1. In TrueNAS, open **Apps**, select `jobmonitor`, and open its **logs** (in the **Workloads**
   section, use the logs button next to the container). Find the line:

   ```
   No password is set. Setup token: Xy12AbCd... - open http://<host>:<port>/setup to choose one.
   ```

2. Open the app in your browser. You're sent to the setup page. Paste the token, choose a password
   of at least 12 characters, and you're in.

To skip this, set `APP_PASSWORD` in the YAML before the first start. It's only used if no password
has been set yet.

## Add a monitor

1. Go to **Monitors > Add a monitor**.
2. **Careers page:** paste the address of the page that *lists* the jobs (not a single posting) and
   click **Find jobs**. You'll see how many postings were found and how they were read.
3. **What you're looking for:** enter words or phrases separated by commas. Matching ignores upper
   and lower case and only counts whole words, so "java" doesn't match "JavaScript". The preview
   updates as you type and shows why each posting matches. Leave everything empty to be told about
   every new posting.
4. **Name and schedule:** pick how often to check (every 12 hours by default). Checks spread out by a
   few minutes either way.
5. Click **Save monitor**. The first check runs straight away and records what's listed now. From
   then on, each new posting that matches appears on the **Dashboard**, where you can open it, mark
   it seen, or dismiss it.

If Job Monitor can't find postings on a page, open **Advanced**. You can force the browser, or
describe the page with CSS selectors (one for each posting, plus optional ones for its title, link
and location).

## Notifications

In **Settings > Notifications**, add one or more targets. Each is an
[Apprise URL](https://github.com/caronc/apprise/wiki), for example:

| Service | Example URL |
|---|---|
| ntfy (ntfy.sh or self-hosted) | `ntfy://my-job-alerts` or `ntfys://ntfy.example.com/jobs` |
| Discord | `discord://webhook_id/webhook_token` |
| Email (Gmail app password) | `mailtos://you:app-password@gmail.com` |
| Pushover | `pover://user_key@app_token` |
| Any webhook (JSON) | `json://homeassistant.lan:8123/api/webhook/jobs` |

Use **Send test** to check a target. When a check finds new matches you get **one message listing
them**, not one per posting. The first check of a monitor never notifies. Each monitor has a
**Send notifications for new matches** switch, and each target can be paused.

Notification URLs often contain tokens or passwords. They're stored encrypted, and once saved they're
only ever shown with the password hidden. The encryption key is the file `notify.key` in the data
folder, kept separate from the database.

## Updating

Releases are published as `ghcr.io/bhager42/jobmonitoring` with tags `1.2.3`, `1.2`, `1` and `latest`
(and `-lite` versions of each). With the `:1` tag from the YAML, update from **Apps > jobmonitor >
Update** (or **Edit > Save** to pull the newest 1.x image).

On start, the app updates its database automatically. Before changing an existing database it saves a
copy to `backups/pre-migrate-<time>.db` in the data folder. If you ever start an older image on a
database a newer one has upgraded, it refuses to start and says so, instead of damaging it.

## Backups and restore

- **Automatic:** a backup is saved every day to `backups/daily-<time>.db` in the data folder. The
  newest 7 are kept (`BACKUP_KEEP`).
- **On demand:** **Settings > Backups > Download a backup now.**
- **Snapshots:** TrueNAS snapshots of the dataset capture everything, including `notify.key`.

To restore: stop the app, replace `jobmonitor.db` in the data folder with a backup file (and delete
any `jobmonitor.db-wal` and `jobmonitor.db-shm` next to it), then start the app. Keep `notify.key` from
the same install. Without it, notification targets show as "saved by another install" and need to be
added again. Everything else is unaffected.

## Remote access

Job Monitor is meant for your home network. To use it away from home, don't forward its port on
your router. Instead use the TrueNAS **Tailscale** app, or put it behind a reverse proxy with HTTPS
(Caddy, Nginx Proxy Manager, Traefik). Behind an HTTPS proxy, set:

```yaml
      SECURE_COOKIES: "true"   # cookies only over HTTPS (and HSTS)
      TRUST_PROXY: "true"      # use the proxy's X-Forwarded-For / -Proto headers
      FORWARDED_ALLOW_IPS: "172.16.0.0/12"   # the proxy's address(es); not "*"
```

## Configuration

Set these as `environment` entries in the YAML. Everything else is configured in the app.

| Variable | Default | What it does |
|---|---|---|
| `TZ` | `UTC` | Time zone for the times shown in the app |
| `APP_PASSWORD` / `APP_PASSWORD_FILE` | not set | Initial password (ignored once a password exists) |
| `SECURE_COOKIES` | `false` | `true` behind HTTPS: secure cookies and HSTS |
| `TRUST_PROXY` | `false` | Trust `X-Forwarded-*` headers from a reverse proxy |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | Which proxy addresses to trust |
| `MAX_CONCURRENT_CHECKS` | `3` | Checks that may run at the same time |
| `DEFAULT_INTERVAL_HOURS` | `12` | Pre-filled interval for new monitors |
| `BACKUP_KEEP` | `7` | How many daily backups to keep |
| `ALLOW_PRIVATE_URLS` | `false` | Allow monitoring pages on private/local addresses (see below) |
| `LOG_LEVEL` | `info` | `debug`, `info`, `warning` or `error` |
| `PORT` | `8080` | Port inside the container (change the host port in `ports` instead) |
| `OUTBOUND_PROXY`, `OUTBOUND_CA_BUNDLE` | not set | Send page fetches through an HTTP proxy (advanced) |

## Troubleshooting

**The app keeps restarting and the log says "data directory /data is not writable by UID 568".**
The **apps** user can't write to the dataset. Edit the dataset's permissions (Install step 1.3) so
**apps (568)** has Modify, then restart the app.

**The app won't start: "port is already allocated" or similar.** Another app uses port 30880. Edit the
app and change the first number in `"30880:8080"` to a free port, e.g. `"30881:8080"`, then open that
port instead.

**I lost the setup token.** Restart the app. A new token is printed each time it starts without a
password.

**I forgot my password.** Open a shell in the container (**Apps > jobmonitor**, then in **Workloads**
use the shell button next to the container) and run:

```bash
jobmonitor reset-password
```

It asks for the new password twice and signs out every browser.

**Too many failed sign-ins.** After 5 wrong passwords from one address in 15 minutes, that address
is locked out for 15 minutes. Wait, or reset the password as above.

**"No job postings found on that page."** Make sure the address is the page that *lists* the jobs.
Many companies link to their hiring service (e.g. `boards.greenhouse.io/...` or
`...myworkdayjobs.com/...`); that address usually works best. If the list only appears after the
page runs JavaScript, use the full image (not `-lite`) and set **Advanced > How to read the page** to
**Always use the browser**. As a last resort, describe the page with CSS selectors.

**A monitor says "Blocked".** The site refused the request, either through its `robots.txt` or with
bot protection. Job Monitor obeys `robots.txt` and doesn't try to get around bot protection. Try the
company's hiring-service URL instead. **Advanced > Obey the site's robots.txt** can be turned off
for sites you're allowed to check.

**"Refusing to fetch ... non-public address".** For safety, Job Monitor won't fetch addresses on your
own network (your router, the NAS itself and so on) unless `ALLOW_PRIVATE_URLS` is `true`. Enable it
only if you really need to monitor an internal careers page.

**A monitor shows a warning: "Found 0 postings (previously N)".** The page suddenly listed nothing,
which usually means it changed rather than every job being filled. Postings aren't marked as removed
until it recovers. Run **Find jobs** again on the edit page to check how it reads now.

**"Checking less often after repeated failures".** After 5 failed checks in a row, the interval
doubles with each further failure, up to a week. It returns to normal after the next successful check.

**Notifications don't arrive.** Use **Send test** in Settings, then look at the app log for the error.
The Settings page also shows the last failure.

## Development

See [`CLAUDE.md`](CLAUDE.md) for commands and conventions, [`docs/DEVELOPMENT_PLAN.md`](docs/DEVELOPMENT_PLAN.md)
for the design, and [`CHANGELOG.md`](CHANGELOG.md) for releases.

```bash
uv sync --all-extras
uv run pytest
DATA_DIR=./data uv run jobmonitor serve       # http://localhost:8080
docker compose -f compose.dev.yaml up --build # the hardened container
```

**Releasing:** update `app/__init__.py` and `CHANGELOG.md`, merge, then push a tag
(`git tag v1.2.3 && git push origin v1.2.3`). The **Release** workflow runs all checks against both
images, then publishes them to GHCR. The first time, make the package public under the repository's
**Packages** settings (or add a registry credential in TrueNAS under **Apps > Configuration > Manage
Container Image Registries**).
