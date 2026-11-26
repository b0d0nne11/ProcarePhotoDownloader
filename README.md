# Procare Photo Downloader

> Unofficial and unaffiliated with Procare. See the [disclaimer](#disclaimer).

Procare Connect only lets you save photos one at a time. This tool walks the
same API the web app uses and downloads every photo and video for each of your
children into a plain, browsable folder tree. File timestamps are set to when
the photo was taken, so the library sorts correctly in Finder, Photos, or any
backup tool.

```
photos/
  Doe, John/
    2026/
      2026-08-16_094653_f8409979.jpg
      2026-08-14_112035_a31c0b7e.heic
    2025/
      ...
  Doe, Jane/
    ...
```

## Install

```sh
uv sync
```

## Use

```sh
# Authenticate once. The token is cached under your platform's config directory.
procare-photos login

# See which children are on the account.
procare-photos kids

# Download everything.
procare-photos --out ~/Pictures/Procare sync

# Just one child, just this year.
procare-photos --out ~/Pictures/Procare sync -k John --since 2026-01-01

# See what is available without downloading it.
procare-photos --out ~/Pictures/Procare sync --dry-run

# What do I already have?
procare-photos --out ~/Pictures/Procare status

# Captions and metadata as a spreadsheet.
procare-photos --out ~/Pictures/Procare export --to captions.csv
```

`--out` (`-o`) picks the library and defaults to `./photos`.

`sync` is incremental and safe to re-run. Anything already downloaded is
skipped, so a routine run only fetches what is new. If a run is interrupted,
re-running picks up where it left off.

A plain `sync` re-scans the whole history every time, which is thorough but
takes several minutes because of Procare's rate limit. For a routine top-up,
bound the scan instead:

```sh
procare-photos --out ~/Pictures/Procare sync --since $(date -v-30d +%Y-%m-%d)
```

### Useful flags

| Flag | Effect |
| --- | --- |
| `--kid`, `-k` | Limit to one child, by name fragment or id. Repeatable. |
| `--since` / `--until` | Restrict the date range (`YYYY-MM-DD`). |
| `--no-videos` | Skip videos. |
| `--no-activity-photos` | Skip photos attached to timeline activities. |
| `--dry-run` | Report what is available without downloading. |
| `--concurrency` | Parallel downloads, default 8. |
| `--rate` | Ceiling on API requests per second, default 1.5. |

### Repairing a library

These commands edit the catalog without touching the network. Both report how
many entries they updated and the next `sync` fetches them.

| Command | Effect |
| --- | --- |
| `verify` | Re-queue items the catalog lists but whose files are gone. |
| `reset-failed` | Clear the failure count on items that were given up on. |

## Credentials

In order of precedence:

1. `--email` / `--password` on `login`
2. `PROCARE_EMAIL` and `PROCARE_PASSWORD` environment variables
3. A `.env` file in the directory you run the command from
4. An interactive prompt

Each half is resolved on its own, so setting only `PROCARE_EMAIL` and being
prompted for the password is valid. The `.env` file is read from the current
working directory.

Only the auth token is written to disk in a platform-dependent location
(`~/Library/Application Support` on macOS, `~/.config` on Linux,
`%LOCALAPPDATA%` on Windows). Setting `$XDG_CONFIG_HOME` overrides it on macOS
and Linux. Your password is never stored.

## How it works

The Procare web app is a single-page app backed by an undocumented JSON API.
Five endpoints are relevant to this project:

| Call | Purpose |
| --- | --- |
| `POST /api/web/auth/` | Exchange email and password for an auth token |
| `GET /api/web/parent/kids` | List children on the account |
| `GET /api/web/parent/photos` | List photos for a child in a date range |
| `GET /api/web/parent/videos` | List videos for a child in a date range |
| `GET /api/web/parent/daily_activities` | The timeline behind the dashboard |

Media files themselves are served from a CloudFront distribution using
pre-signed URLs.

### Constraints this tool works around

None of these are documented; they were measured against the live API.

- **Page size caps at 50** no matter what `per_page` is requested.
- **Wide date ranges time out.** A 90-day window returns in about a second. A
  one-year window reliably returns HTTP 504 after 60 seconds. The sweep uses
  60-day windows and halves any window that times out, down to a single day.
- **The API rate limits, and says nothing about how.** There are no rate limit
  headers, and the behaviour looks like a quota per time window rather than a
  steady rate. A fixed request rate therefore either crawls or wastes requests
  on refusals. The limiter adapts the way TCP does: halve on a 429, edge back up
  while succeeding. `--rate` sets the ceiling, not a fixed pace.
- **Signed media URLs last 90 days.** Long enough that listing and downloading
  can be separate phases and the catalog can store URLs for later retries.
- **Half the photos are not in the gallery.** Staff attach photos to a timeline
  entry as often as they post them to the gallery and `/parent/photos` never
  returns those. They come back on the activity's own `photo_url` from a
  different CDN bucket. They do not migrate, i.e., photos from three months ago
  still sit on dates the gallery has nothing at all for. The sweep therefore
  walks the timeline as well. These arrive smaller than a gallery original,
  around 390x600 against 600x800, and there is only the one URL with no larger
  variant to ask for. The timeline reports gallery media as well via
  `photo_activity` and `video_activity` entries so those are skipped. The
  timeline is a third kind of media alongside photos and videos so
  `--no-activity-photos` leaves it out of the sweep the same way `--no-videos`
  does and `status` reports it as its own row.
- **Photos are shared between siblings.** A group photo comes back under every
  child in it, under the same id. Each child gets their own copy in their own
  folder, so the catalog is keyed by child and photo together.
- **Some media is simply gone.** A meaningful share of older items return HTTP
  403 from the CDN with a freshly issued, still-valid signature, which means the
  underlying file is missing on Procare's servers. These fail in Procare's own
  web app too. The tool retries such items three times across runs, then stops
  and reports them as unavailable rather than burning requests on them forever.
  `procare-photos reset-failed` starts the count over.

### Sweeping backwards

`sync` walks backwards from today in 60-day windows, per child.

`/parent/kids` reports a `created_at` for each child so the sweep stops there.
Siblings enrol years apart, which is why the floor is applied per child rather
than once to `--since`. Without that date the sweep runs back to 2015, which is
slow but complete.

`--since` still wins where it is the later of the two. It raises the start of
the sweep and never widens it.

### Local catalog

Each output directory holds a `.procare-catalog.sqlite3` index of every item
seen, its caption, where it was saved, and why it failed if it did. This is what
makes sync incremental and resumable. Each file's location is recorded relative
to the output directory so that the catalog is portable.

Deleting it is harmless; the next sync rebuilds it and adopts the files already
on disk rather than re-downloading them.

## Development

`uv sync` installs the development dependencies along with everything else.

```sh
uv run pytest
uv run --with ruff ruff check src tests
uv run ty check
```

After changing a table in `models.py`, generate the matching migration:

```sh
uv run alembic revision --autogenerate -m "what changed"
```

## Disclaimer

This is an unofficial personal project. It is not affiliated with, endorsed
by, or supported by Procare Solutions. "Procare" and "Procare Connect" are
their trademarks and are used here for identification only.

It relies on an undocumented API that can change without notice, so it may
stop working at any time. Use it at your own risk, and only with an account
you are entitled to use: it uses your own credentials to fetch your own
children's photos, and is designed to limit its request rate so as not to
place undue load on Procare's services.
