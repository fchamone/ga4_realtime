# ga4_realtime

A live terminal dashboard for GA4's Realtime report.

GA4's Realtime report only reaches back 30 minutes and the standard reports lag
4–8 hours behind, so traffic that happens while nobody is watching the screen is
lost. This script polls that window on a schedule, accumulates it into SQLite,
and draws a "today so far" view from the accumulated history.

One process, two jobs: a background thread polls and writes, the main thread
renders from the same database.

The code started in `tools/` of the fchamone.com.br repository, next to the site
it measures, and moved here once it outgrew being an operational script. Two
consequences still visible when reading it: the script carries paths and
messages in the old `tools/…` form, and the site stays in the other repository —
nothing here publishes or deploys anything.

## Install

```powershell
py -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Then copy the template and fill it in:

```powershell
Copy-Item .env.example .env
```

**The venv has to exist before the script will run.** It carries a shebang
pointing inside it (`#!.venv\Scripts\python.exe`), and that is what lets
`py ga4_realtime.py` find the right interpreter from any working directory
without activating anything. Without the venv, `py` fails with a raw error from
the launcher itself instead of the script's explanatory message.

**Use `py`, never `python`.** On Windows, `python` on its own is a 0-byte
shortcut to the Microsoft Store
(`%LOCALAPPDATA%\Microsoft\WindowsApps\python.exe`) and answers *"Python was not
found"* even with Python installed. Turning the alias off in Settings → Apps →
Advanced app settings → **App execution aliases** only makes the error honest:
the real Python's directory is not on `PATH`, only its `Scripts` is.

`requirements.txt` pins the whole tree (`pip freeze`), not just the direct
dependencies: with no lockfile, the freeze is what makes a future install
reproduce this one. To update it, install whatever you want into the venv and
freeze again.

## GA4 access (once)

The script reads the GA4 APIs with a service account. Five steps, all outside
the repository:

1. [Google Cloud Console](https://console.cloud.google.com/) → create or pick a
   project → **APIs & Services** → enable the **Google Analytics Data API** and
   the **Google Analytics Admin API** as well. They are two distinct APIs: the
   second is the one that returns the property's timezone, and without it
   `ga4_realtime.py` warns and falls back to UTC.
2. **IAM & Admin → Service accounts** → create a service account. It needs no
   role at all on the project: the permission that matters is step 4.
3. On the account just created → **Keys → Add key → Create new key → JSON**.
   Save the file as `secrets/ga4-service-account.json`.
4. GA4 → **Admin → Property access management** → add the service account's
   e-mail (`something@project.iam.gserviceaccount.com`) with the **Viewer**
   role. Without this the API answers `PermissionDenied` even with the right
   credential.
5. GA4 → **Admin → Property settings** → copy the **Property ID** (numeric) into
   `GA4_PROPERTY_ID` in `.env`.

Step 5 is where almost everyone trips: the property ID is a number. The
`G-XXXXXXXXXX` in the site's HTML is the *measurement ID*, which is for
`gtag.js` and is no use to the API.

## Usage

```powershell
py ga4_realtime.py                              # live view
py ga4_realtime.py --interval 60                # poll faster
py ga4_realtime.py --window 12                  # widen the chart's axis
py ga4_realtime.py --ascii --verbose
py ga4_realtime.py report --date 2026-08-05
py ga4_realtime.py export --format csv
```

`py` works from any working directory: the shebang points at
`.venv\Scripts\python.exe`, and the Windows launcher resolves an unrecognised
shebang relative to the *script's* directory rather than the current one. The
explicit `.venv\Scripts\python.exe ga4_realtime.py …` form still works and is
what to use anywhere the launcher is unavailable. Bare `python` does not work —
see **Install** above.

`q` or Ctrl-C quits; both join the polling thread before exiting. Data is
written on every poll, so quitting never loses what was already collected.

`report` and `export` read only the local database: the property's name and
timezone come from what the live view cached there, so neither needs
credentials or a network round trip.

## What it records

Each poll issues one Realtime request over the last 10 minutes — a window
deliberately wider than the 5-minute default interval, so late-arriving events
and skipped cycles are re-read and corrected rather than lost. It is broken down
by `eventName`, `deviceCategory` and `country`, for `screenPageViews` and
`eventCount` — the two additive metrics.

Everything lands in `out/ga4_realtime.db` (`--db` overrides). Rows are keyed on
`(minute, event, device, country)` and **upserted**: re-polling a minute already
seen *corrects* the stored row instead of adding to it. This is the correctness
property the whole design hangs on, so the UPSERT overwrites every metric column
and never accumulates.

## Two things worth knowing before reading the numbers

**There is no user count, of any kind.** The same visitor appears in
consecutive minute windows, and the Realtime API exposes no identifier to
deduplicate on — so `activeUsers` cannot be summed across minutes. Worse, in the
fan-out one visitor firing `page_view`, `scroll` and `session_start` would show
up in three rows of the *same* minute, so it could not be summed across rows
either. It is a concurrency reading and nothing more. There is no honest daily
unique-user count in realtime data, so the tool does not print one.

A second request once collected that gauge per minute, into its own
`realtime_users` table, and the dashboard drew it as a chart. Both are gone: the
reading was worth less than the vertical space it cost, and dropping it halves
the API calls per poll. Databases from before the change keep the table,
orphaned and unread — deleting it would destroy collected history to reclaim a
few kilobytes.

Google enforces this rather than merely documenting it: asking for
`activeUsers` alongside `eventName` is rejected with *"Selected dimensions and
metrics cannot be queried together"*, because one is user-scoped and the other
event-scoped. So the fan-out table has no `active_users` column at all — there
is nothing there to sum by mistake.

**There are no page paths.** The Realtime API has no `pagePath` dimension —
only `unifiedScreenName`, which on web returns the page *title*. On a one-page
site that is close to useless, so the breakdown is by `eventName` instead,
which has the side benefit of putting `generate_lead` on screen live.

## Notes

- Day boundaries use the **property's** timezone, never the machine's, fetched
  from the Admin API at startup. `--timezone` overrides it. This is what
  `tzdata` is in `requirements.txt` for.
- The cumulative chart marks every minute in which `generate_lead` fired with a
  `*` along the baseline, so the conversion is legible against the traffic that
  produced it. The y axis is whole numbers — page views are counted, not
  measured, and an axis labelled `5.50` claims half a page view happened. The
  line runs out to the current minute: while the poller is live, a minute with
  no rows really did have no page views. If polling stalls, that flat run means
  "not collected" instead — which is what the header's reddening `last poll …
  ago` is there to say.
- **The chart's x axis is a trailing window ending now — 6 hours by default,
  `--window HOURS` (1–24) to change it — not a fit to the recorded data.**
  Fitting the axis to the data stretched ten minutes of a fresh run across the
  whole panel and rescaled the chart on every poll; a fixed window slides
  forward with the clock instead and stays still. Two things follow. The y
  values remain cumulative since local midnight, so later in the day the curve
  enters the window at a height rather than at zero — the level it enters at is
  carried from the last point before the window, not interpolated. And before
  roughly 06:00 the left of the axis is empty, because the window reaches back
  past midnight while the series is day-scoped; that is preferred over clamping
  it to midnight, which would put the stretched-out look back for the first
  hours of every day. Everything else — the footer totals, the events table,
  `report` — stays day-scoped and ignores the window.
- The tool only ever reports. Thresholds and alerting wait until `out/` holds
  enough history to calibrate against real numbers.
- Logs go to `out/ga4_realtime.log` (rotating), never to stdout — stdout
  belongs to the live view. `--verbose` adds a log panel to the layout.
- Piping or redirecting stdout skips the live view and prints the plain daily
  summary instead. `--ascii` drops to plain ASCII everywhere, and is enabled
  automatically if the console encoding cannot represent block characters.
  `NO_COLOR` is respected.

## Conventions

**One file per task, at the root of the repository.** Anything reusable moves
down into a package (`fcpy/`, which does not exist yet). This is not taste:
CPython puts the script's own directory on `sys.path[0]`, so `ga4_realtime.py`
imports its neighbour from any working directory, with no `pip install -e .` and
no `pyproject.toml`. A script inside a subdirectory loses that and starts
requiring packaging.

**Paths resolve from `__file__`, never from `cwd`.** Windows Task Scheduler runs
with the working directory at `C:\Windows\System32`, and anything relative to
`cwd` breaks there. The constants at the top of the script (`TOOLS_DIR`,
`OUT_DIR`, `DEFAULT_DB`, `LOG_PATH`) exist for that.

**Secrets stay out of git.** `.env` and everything in `secrets/` are ignored;
`.env.example` is the only one kept under version control, and it is what
documents the keys. When you add a new variable, add it to `.env.example` too.

**Generated artifacts go to `out/`**, also ignored.
