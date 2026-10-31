# Changelog

All notable changes to Job Monitor. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions follow [Semantic Versioning](https://semver.org/).

## [1.0.0] - 2026-10-29

First release, licensed under the GNU Affero General Public License v3.0 or later.

### Added
- **Monitors** for company careers pages. Greenhouse, Lever, Ashby, SmartRecruiters and Workday
  boards are read through their public feeds. Other pages are read through schema.org job data, job
  links, or your own CSS selectors, with an optional headless browser for pages that need JavaScript.
- **Find jobs** preview while adding a monitor, and a **live match preview** that shows which
  postings match and why as you type.
- **Matching** on any/all/none of your keywords (whole words, case- and accent-insensitive), title
  pattern, locations, remote only, departments, employment type and posting age.
- **Scheduled checks** with a per-monitor interval, ±10% jitter, a few at a time, and back-off for
  sites that keep failing. **Check now** runs one immediately.
- **Baseline first check:** existing postings are recorded, not alerted. Later postings that match
  appear on the dashboard. Postings missing from two checks in a row are marked as no longer listed,
  and a page that suddenly lists nothing triggers a warning instead.
- **Notifications** through Apprise (ntfy, Discord, email, Pushover, webhooks and more): one digest
  per check, with a per-monitor switch, Send test, and encrypted storage of the notification URLs.
- **Web interface** for the dashboard, monitors, matches and settings. Light and dark mode follow the
  device. It works on phones and without JavaScript, and can be installed as an app (PWA).
- **Security:** one password (Argon2id) with first-run setup token, login lockout, CSRF protection,
  strict Content-Security-Policy, and protection against fetching private network addresses.
  Follows `robots.txt`.
- **Data:** embedded SQLite with automatic migrations, a backup before each migration, daily
  backups, and a backup download.
- **TrueNAS SCALE 25.10:** an Install via YAML file (`docs/truenas-app.yaml`). The container runs as
  the apps user (568) with a read-only root filesystem.
- **Images** `ghcr.io/bhager42/jobmonitoring` (with browser) and `-lite` (without), for linux/amd64.
- `jobmonitor` command: `serve`, `migrate`, `reset-password`, `probe <url>`.

[1.0.0]: https://github.com/bhager42/jobmonitoring/releases/tag/v1.0.0
