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

1. Go to **Apps**. If asked, choose the pool for apps.
2. Open **Discover Apps**, click the three-dot menu at the top right, and choose
   **Install via YAML**.
3. Name the app `jobmonitor` and paste the YAML below (the same as
   [`docs/truenas-app.yaml`](docs/truenas-app.yaml)). Change the line marked `CHANGE`: set `TZ` to
   your [time zone](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones), written with
   underscores, e.g. `America/New_York` or `Europe/London`. An invalid name silently falls back to
   UTC.
4. Click **Save**. TrueNAS pulls the image and starts the app. Wait until it shows **Running**.

<!-- truenas-app.yaml: start (generated from docs/truenas-app.yaml; a test keeps them identical) -->
```yaml
# Job Monitor for TrueNAS SCALE 25.10: Apps > Discover Apps > (three-dot menu) > Install via YAML.
# Before installing, change the line marked CHANGE. See the README for step-by-step help.
services:
  jobmonitor:
    # ":latest" follows the newest release, including future major versions. Use ":1" to stay
    # on 1.x, or ":latest-lite" / ":1-lite" for a smaller image without the browser (careers
    # pages that need JavaScript won't work).
    image: ghcr.io/bhager42/jobmonitoring:latest
    restart: unless-stopped
    init: true
    user: "568:568"                         # TrueNAS's "apps" user and group
    ports:
      - "30880:8080"                        # host:container. Change 30880 if it's taken.
    environment:
      TZ: "America/New_York"                # CHANGE to your time zone (note the underscores)
      SECURE_COOKIES: "false"               # "true" only behind an HTTPS reverse proxy
      TRUST_PROXY: "false"                  # "true" only behind a reverse proxy
      # APP_PASSWORD: "at least 12 characters"   # optional; otherwise use the setup token in the logs
    volumes:
      # A Docker volume that TrueNAS manages: works with no setup. Deleting the app may delete it,
      # so download a backup first (Settings > Backups).
      - jobmonitor:/data
      # Or keep the data in a dataset of your own (for your own snapshots): replace the line
      # above with the one below, and give the "apps" user (568) Modify permission on the dataset.
      # - /mnt/tank/apps/jobmonitor:/data
    shm_size: "512m"                        # room for the headless browser
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
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 30s

volumes:
  jobmonitor:
```
<!-- truenas-app.yaml: end -->

The app keeps its data in a Docker volume that TrueNAS manages, so there's nothing to set up first.
**Deleting the app in TrueNAS may delete that volume too**, so download a backup first
(**Settings > Backups**).

The app listens on port **30880**: open `http://<your-truenas-address>:30880`.

The file uses image tag `:latest`, which follows the newest release, including future major
versions (which may bring breaking changes). To stay on 1.x, use `:1` instead. There are also smaller
`-lite` images (`:latest-lite`, `:1-lite`) without the headless browser. They work for most sites,
but can't read careers pages that only show jobs after running JavaScript.

### Optional: keep the data in your own dataset

Use this if you want the data in a dataset you control, for example to include it in your own
snapshot schedule.

1. **Datasets > Add Dataset**: name it `jobmonitor` and set **Dataset Preset** to **Apps**.
2. Select it and, under **Permissions**, click **Edit**. Make sure the **apps** user (UID 568) has
   **Modify** (or Full Control), adding an entry if needed, then save.
3. In the YAML, replace the line `- jobmonitor:/data` with the commented-out
   `- /mnt/tank/apps/jobmonitor:/data`, using your dataset's path.

If the app can't write to the folder, it doesn't start. Instead it shows the problem on its page and
in its log, and TrueNAS marks it unhealthy.

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
(and `-lite` versions of each). With the `:latest` tag from the YAML, update from **Apps >
jobmonitor > Update** (or **Edit > Save** to pull the newest image).

On start, the app updates its database automatically. Before changing an existing database it saves a
copy to `backups/pre-migrate-<time>.db` in the data folder. If you ever start an older image on a
database a newer one has upgraded, it refuses to start and says so, instead of damaging it.

## Backups and restore

- **Automatic:** a backup is saved every day to `backups/daily-<time>.db` in the data folder. The
  newest 7 are kept (`BACKUP_KEEP`).
- **On demand:** **Settings > Backups > Download a backup now.** Do this before deleting the app.
- **Snapshots:** if you keep the data in your own dataset, its TrueNAS snapshots capture everything,
  including `notify.key`.

To restore, put the backup file on the NAS (e.g. through an SMB share), stop the app, and in
**System > Shell** find the data folder:

```bash
sudo docker volume ls | grep jobmonitor
```

```bash
sudo docker volume inspect -f '{{ .Mountpoint }}' <volume name from above>
```

Then replace `jobmonitor.db` in that folder with the backup, remove the database's side files, and
give it back to the apps user (for a dataset, use the dataset's path as the folder):

```bash
cd <mountpoint> && sudo cp /path/to/backup.db jobmonitor.db && sudo rm -f jobmonitor.db-wal jobmonitor.db-shm && sudo chown 568:568 jobmonitor.db
```

Start the app. Keep `notify.key` from the same install. Without it, notification targets show as
"saved by another install" and need to be added again. Everything else is unaffected.

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

**The page says "Job Monitor can't start" and TrueNAS shows the app as unhealthy.** The page and the
log explain why. The most common reason is "data directory /data is not writable by UID 568": the
YAML uses your own dataset, but the **apps** user can't write to it. Give **apps (568)** Modify on the
dataset (see [Optional: keep the data in your own dataset](#optional-keep-the-data-in-your-own-dataset)),
or switch back to the default `- jobmonitor:/data` volume, then restart the app. Other reasons
include an `APP_PASSWORD` shorter than 12 characters.

**Version 1.0.0 cycled between Deploying and Stopped and couldn't be stopped.** That was the same
problem; 1.0.0 exited instead of explaining it. To stop it, open **System > Shell** and run
`sudo docker ps -a | grep jobmonitor`, then `sudo docker update --restart=no <name>` and
`sudo docker stop <name>`. Then update to the latest image and fix the cause.

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

## License

Copyright (C) 2026 bhager42

Job Monitor is free software: you can redistribute it and/or modify it under the terms of the
[GNU Affero General Public License](LICENSE) as published by the Free Software Foundation, either
version 3 of the License, or (at your option) any later version. It is distributed in the hope that
it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
FITNESS FOR A PARTICULAR PURPOSE. See the license for details.

The AGPL also covers network use: if you let other people use a **modified** version over a network,
you must offer them its source code. Every page links to the source in its footer; set
`SOURCE_URL` in `app/__init__.py` to your own copy if you change the code.

Third-party parts keep their own licenses: htmx (0BSD) is included in the app, and axe-core
(MPL-2.0) is used only by the tests. Python dependencies are permissively licensed (MIT, BSD,
Apache-2.0 and similar).

## Development

See [`CLAUDE.md`](CLAUDE.md) for commands and conventions, [`docs/DEVELOPMENT_PLAN.md`](docs/DEVELOPMENT_PLAN.md)
for the design, and [`CHANGELOG.md`](CHANGELOG.md) for releases.

```bash
uv sync --all-extras
uv run pytest
DATA_DIR=./data uv run jobmonitor serve       # http://localhost:8080
docker compose -f compose.dev.yaml up --build # the hardened container
```

**Releasing:** update `app/__init__.py` and `CHANGELOG.md`, merge, then either push a tag
(`git tag v1.2.3 && git push origin v1.2.3`) or open **Actions > Release > Run workflow** and enter
`1.2.3`. The **Release** workflow runs all checks against both images, publishes them to GHCR, and
creates the GitHub release. The first time, make the package public under the repository's
**Packages** settings (or add a registry credential in TrueNAS under **Apps > Configuration > Manage
Container Image Registries**).
