# Changelog

All notable changes to Job Monitor. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions follow [Semantic Versioning](https://semver.org/).

## [1.0.3] - 2026-10-31

### Fixed
- **Changing a saved monitor's criteria now updates its matches.** Before, criteria were applied
  only when a posting was first seen, so removing a filter (e.g. a location) never brought earlier
  results back, and adding one left matches that no longer fit. Saving changed criteria now judges
  the postings listed now again: ones that fit become matches (shown on the dashboard, not sent as
  notifications), and matches that no longer fit are removed. Dismissed matches stay dismissed.

### Changed
- The Edit page previews the criteria live against the postings the last check found, without
  pressing Find jobs.

## [1.0.2] - 2026-10-31

### Fixed
- **"Find jobs" listed menu and footer links as postings** on big-company careers sites such as
  Google's ("Jobs", "How we hire", "Sign in", policy links). The link finder now ignores site
  menus, headers and footers, and only accepts links whose address identifies one job (an ID, or
  a job title after `/jobs/`). Icon-font names ("work_outline") and repeated labels no longer end
  up in titles.
- **Job cards whose link just says "Learn more"** now get the job's title from the link's label or
  the card's heading, instead of being skipped.
- **Pages with a `<base href>`** had their relative job links resolved to the wrong address.

### Changed
- When a page lists no jobs but links to a list ("View all jobs"), the message suggests that
  address.

## [1.0.1] - 2026-10-31

### Fixed
- **No more restart loop on TrueNAS when the app can't start.** If the data folder isn't writable
  (the usual cause), the database can't be upgraded, or `APP_PASSWORD` is invalid, the app now stays
  running and shows the problem on its page and in its log, and `/healthz` reports it, so TrueNAS
  marks the app unhealthy. Before, it exited, and the restart policy made the app cycle between
  Deploying and Stopped in a way TrueNAS couldn't stop.
- **Adding a monitor failed with "incorrect header check" on sites that compress their pages**
  (gzip or deflate), such as Google's and Lenovo's careers sites. The page was decompressed twice;
  now it is decoded once.

### Changed
- The TrueNAS install file (`docs/truenas-app.yaml`) now keeps the data in a **Docker volume that
  TrueNAS manages**, so it works with no dataset or permission setup. Keeping the data in your own
  dataset is still supported, as a commented-out line in the file and in the README.
- The install file uses the **`:latest`** image tag, so it follows every release. To stay on 1.x, use
  `:1`. The smaller images are `:latest-lite` and `:1-lite`.
- The README's install, backup/restore and troubleshooting sections match the new defaults. The
  example time zone shows the underscore format (`America/New_York`).

## [1.0.0] - 2026-10-31

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

[1.0.1]: https://github.com/bhager42/jobmonitoring/releases/tag/v1.0.1
[1.0.0]: https://github.com/bhager42/jobmonitoring/releases/tag/v1.0.0
