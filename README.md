# ga4-realtime

A live terminal dashboard for GA4's Realtime report, for one property or
several at once.

GA4's Realtime report reaches back only 30 minutes, and the standard reports
lag 4 to 8 hours. Traffic that happens while nobody watches is lost. This tool
polls the realtime window on a schedule, stores every poll in SQLite, and draws
a "today so far" view from the stored history.

One process does two jobs. One polling thread per site writes to the database,
and the main thread renders from it. The dashboard shows one site at a time,
and `Tab` moves between sites.

**Collection stops when the dashboard stops.** Nothing runs in the background,
as a service, or on a timer. See [Limits](#limits).

You need Python 3.11 or newer, a GA4 property, and permission to create a
service account in the Google Cloud project behind it.

---

## Install

With [pipx](https://pipx.pypa.io/), which keeps the tool and its dependencies
out of your other environments:

```bash
pipx install git+https://github.com/fchamone/ga4_realtime
```

Or with `pip`, into the environment you choose:

```bash
pip install git+https://github.com/fchamone/ga4_realtime
```

The package is not on PyPI yet. When it is, drop the URL:
`pipx install ga4-realtime` or `pip install ga4-realtime`.

From a clone, if you want to read or change the code:

```bash
git clone https://github.com/fchamone/ga4_realtime
cd ga4_realtime
python3 -m venv .venv                                   # macOS, Linux
.venv/bin/python -m pip install -e .
```

```powershell
git clone https://github.com/fchamone/ga4_realtime
cd ga4_realtime
py -m venv .venv                                        # Windows
.venv\Scripts\python.exe -m pip install -e .
```

Every route gives you one command, `ga4-realtime`, identical on Linux, macOS
and Windows. It runs from any directory and needs no virtualenv activation.
`python -m ga4_realtime …` does the same thing, if you prefer to name the
interpreter. Check the install:

```bash
ga4-realtime --version
```

---

## Five minutes to a dashboard

```bash
ga4-realtime init      # 1. write a starter config here
                       # 2. fill in two fields
ga4-realtime doctor    # 3. check them, without polling anything
ga4-realtime           # 4. the dashboard
```

**1.** `ga4-realtime init` writes `ga4-realtime.toml` and `.env.example` into
the current directory. It refuses to overwrite either file, and names the file
it would have destroyed. `--path` writes somewhere else, for example
`ga4-realtime init --path ~/.config/ga4-realtime/config.toml`, where every
working directory can find it.

**2.** Two fields in that file are left for you:

```toml
[defaults]
credentials = "secrets/ga4-service-account.json"   # where you saved the key

[[sites]]
name        = "mysite"
property_id = ""                                   # the NUMERIC id
```

The config refuses to load until both are set, and the message names what to
fix. The next section says where the two values come from.

While you are in the file, set `conversions` to the events your property
reports, spelled exactly as GA4 spells them. The starter config guesses
`["purchase", "sign_up"]`.

**3.** `ga4-realtime doctor` checks each site: config valid, credentials
readable, Data API reachable, Admin API reachable or the UTC fallback,
timezone resolvable. It also prints the resolved database and log paths. It
writes nothing and creates neither file. It exits 1 when it finds a problem,
so a script can use it.

**4.** `ga4-realtime` opens the dashboard. Press `q` to quit.

---

## GA4 access (once)

The tool reads the GA4 APIs with a service account. Five steps, all outside
this repository:

1. [Google Cloud Console](https://console.cloud.google.com/) → create or pick
   a project → **APIs & Services** → enable the **Google Analytics Data API**
   and the **Google Analytics Admin API**. The Admin API returns the
   property's display name and timezone; without it the tool warns and uses
   UTC day boundaries.
2. **IAM & Admin → Service accounts** → create a service account. It needs no
   role on the project. Step 4 grants the permission that matters.
3. On that account → **Keys → Add key → Create new key → JSON**. Save the file
   where your config's `credentials` key points, by default
   `secrets/ga4-service-account.json`, relative to the config file.
4. GA4 → **Admin → Property access management** → add the service account's
   e-mail (`something@project.iam.gserviceaccount.com`) with the **Viewer**
   role. Without this the API answers `PermissionDenied`.
5. GA4 → **Admin → Property settings** → copy the numeric **Property ID** into
   `property_id` in your config file.

Step 5 catches most people. The property ID is a number. The `G-XXXXXXXXXX`
value in the site's HTML is the *measurement ID*, which `gtag.js` uses and the
API does not accept. The config rejects a `G-` value by name, before any call.

One service account can serve every site, or each site can name its own key.
Sites that share a key share one API client.

---

## Using it

```bash
ga4-realtime                                   # the live dashboard
ga4-realtime --site clientb                    # open focused on one site
ga4-realtime --interval 60 --window 12         # poll faster, wider chart
ga4-realtime --ascii --verbose

ga4-realtime report                            # today, every site
ga4-realtime --site clientb report --date 2026-08-05
ga4-realtime sites                             # what is configured and stored
ga4-realtime doctor
ga4-realtime init --path ~/.config/ga4-realtime/config.toml

ga4-realtime --db /path/to/copy.db report      # a database from elsewhere
ga4-realtime --db /path/to/copy.db sites
```

| Command | What it does |
|---|---|
| *(none)* | the live dashboard: poll, store, render, until you quit |
| `report` | the daily summary as plain text, from the local database alone |
| `sites` | every site as enabled, disabled or orphaned, or simply stored when `--db` names a database and no config says which |
| `init` | write a starter config and `.env.example`; never overwrites |
| `doctor` | check config and API access per site; polls nothing, writes nothing |

**Only the dashboard and `doctor` reach the API.** `report` and `sites` read
the local SQLite file. Each site's property name and timezone come from what
the live view cached there, so both commands work from `--db` alone, with no
config file, no credentials and no network. That is what makes a database
copied off another machine readable.

**Piping or redirecting stdout skips the live view.** `ga4-realtime | cat`
still authenticates and primes every site with one poll, then prints exactly
what `ga4-realtime report` prints and exits. A script that parses one parses
the other. It is the cheapest end-to-end check.

### Global flags

One spelling works on `ga4-realtime` itself, on every subcommand, and on
either side of the subcommand word. `ga4-realtime --db copy.db report` and
`ga4-realtime report --db copy.db` are the same line. Type a flag on both
sides and the last one wins. The dashboard has no subcommand word, which is
why these flags are global.

| Flag | Effect |
|---|---|
| `--config PATH` | the config file to read; otherwise the search below |
| `--site NAME` | which site to act on; `all` means every site |
| `--db PATH` | the SQLite file, relative to the current directory |
| `--interval SECONDS` | seconds between polls |
| `--refresh SECONDS` | seconds between redraws |
| `--window HOURS` | hours of history on the chart's x axis, 1 to 24 |
| `--timezone ZONE` | force every site's day boundaries into one zone |
| `--ascii` | plain ASCII plots and bars |
| `--verbose` | add a log panel to the dashboard, and log at debug level |
| `--version` | print the version and exit |

`report` adds `--date YYYY-MM-DD`, and `init` adds `--path`.

What `--site` means per command:

| | `--site NAME` | `--site all` | omitted |
|---|---|---|---|
| dashboard | opens focused on NAME | same as omitted | opens on the first enabled site |
| `report` | that site only | every site, then a combined total when there is more than one | same as `all` |
| piped stdout | that site's summary | every site's summary | every site's summary |
| `sites`, `doctor`, `init` | accepted and ignored; each always covers everything it knows about | | |

The dashboard needs a site that the *config* knows and has enabled. `report`
and `sites` accept any site the database has recorded, orphans included.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | something you can fix, printed without a traceback, naming the file to edit and the fix. `doctor` also exits 1 when it finds a problem |
| 2 | the arguments were wrong (argparse's own convention) |
| 130 | interrupted with Ctrl-C |

### Keys in the live view

| Key | Action |
|---|---|
| `q`, `Ctrl-C` | quit; every polling thread is joined first |
| `Tab`, `n` | next site |
| `p` | previous site |
| `1`–`9` | jump to site N, in the order the config lists the enabled sites |
| `r` | redraw now, without waiting out the refresh interval |

Every poll writes to the database, so quitting never loses collected data.
`Shift-Tab` is not bound: on Windows it arrives as a two-character extended
sequence, which the key reader discards.

### What is on screen

An identity line (which site, which timezone, the clock, how long ago the last
poll succeeded), a status strip with one marker per site, the cumulative
chart, today's events by count, and a footer of totals.

| Marker | ASCII | Meaning |
|---|---|---|
| `●` | `*` | last poll succeeded, within two intervals |
| `✗` | `!` | last poll failed |
| `○` | `.` | not polled yet, or stale beyond two intervals |

The strip stops a site from failing quietly while you look at another one. The
`--verbose` log panel prefixes each line with its site for the same reason.

The chart's x axis is a trailing window that ends now, 6 hours by default. It
is not a fit to the recorded data. The y values stay cumulative since local
midnight, so later in the day the curve enters the window at a height. A `*`
along the baseline marks every minute in which a configured conversion fired.
The footer totals, the events table and `report` stay day-scoped and ignore
the window.

The rendered frame never exceeds the terminal. When height runs short, the
tool drops the log panel first, then table rows, then the chart. It never
drops the header or the footer.

---

## Configuration

One TOML file. `ga4-realtime init` writes a starter version, and
`ga4-realtime.example.toml` in this repository documents every key.

### Where the file is looked for

Three places, in order, and no more:

1. `--config PATH`, if given. The file exists or the run fails.
2. `./ga4-realtime.toml`, in the current working directory.
3. The platform config directory:
   - Linux/BSD: `$XDG_CONFIG_HOME/ga4-realtime/config.toml`, default
     `~/.config/ga4-realtime/config.toml`
   - macOS: `~/Library/Application Support/ga4-realtime/config.toml`
   - Windows: `%APPDATA%\ga4-realtime\config.toml`

There is no environment variable for it, and the tool does not walk up parent
directories. If it finds nothing, the error names all three places and
suggests `ga4-realtime init`. Place 3 exists because Windows Task Scheduler
runs at `C:\Windows\System32` and a systemd unit runs at `/`.

### Relative paths, one rule

**Every relative path inside the config file resolves against the directory
the config file is in**, never against the directory you ran the command from.
A config in a project keeps its keys and its database inside that project.
`--db` is the one exception: a path typed at a shell resolves against that
shell.

With `[storage] database` unset, the database goes to the platform data
directory: `~/.local/share/ga4-realtime/`, `~/Library/Application
Support/ga4-realtime/`, or `%LOCALAPPDATA%\ga4-realtime\`. Windows uses
`%LOCALAPPDATA%` rather than the roamed `%APPDATA%`, because a SQLite database
in WAL mode is three files that agree only at a single instant. The rotating
log always lives beside the database.

### Per-site keys

Write these under `[defaults]`, where every site inherits them, or inside a
`[[sites]]` block, where that site's value wins.

| Key | Default | Flag | What it is |
|---|---|---|---|
| `credentials` | *required* | — | the service account's JSON key |
| `interval` | `300` | `--interval` | seconds between polls |
| `poll_window` | `10` | — | minutes of realtime history each poll reads, 1 to 30 |
| `conversions` | *required* | — | the events that count as conversions |
| `timezone` | `""` | `--timezone` | IANA name; empty means "ask the Admin API" |
| `label` | Admin API display name | — | what the header calls this site |
| `color` | `[ui] color` | — | this site's chart colour |
| `enabled` | `true` | — | `false`: not polled, not in the `Tab` rotation |

Two keys identify a site, so they belong inside a `[[sites]]` block only:

| Key | What it is |
|---|---|
| `name` | the slug this site is stored and addressed by: lowercase letters, digits, underscore and hyphen, starting with a letter or digit, at most 32 characters. Treat it as immutable: renaming starts a new history, and the old one stays readable under the old name |
| `property_id` | the numeric property ID, **not** the `G-XXXXXXXXXX` measurement ID |

`enabled = false` stops a site being polled and takes it out of the rotation.
Everything it already collected stays readable by `report` and listed by
`sites`.

### Global keys

The dashboard shows one site at a time, so a per-site refresh interval or
chart width would change the furniture under you on every `Tab`.

| Key | Default | Flag | What it is |
|---|---|---|---|
| `[ui] refresh` | `10` | `--refresh` | seconds between redraws; unrelated to `interval` |
| `[ui] window` | `6` | `--window` | hours on the chart's x axis, 1 to 24 |
| `[ui] top_events` | `10` | — | rows the events table shows before it is cropped |
| `[ui] ascii` | `false` | `--ascii` | plain ASCII plots and bars |
| `[ui] color` | `"cyan"` | — | chart colour for sites that name none |
| `[ui] conversion_color` | `"green"` | — | colour of the conversion markers |
| `[storage] database` | platform data directory | `--db` | the SQLite file every site is written to |

All sites share one database file. `site` leads every primary key, so they
never collide, and one file makes a cross-site total a single query.

### A flag beats the file, for every site

**A command-line flag overrides `[defaults]` and every per-site value, for
every site.** `--interval 60` polls every site every 60 seconds, even where a
`[[sites]]` block says 120. `--timezone Asia/Tokyo` forces every site's day
boundaries into Tokyo, which makes two properties in different zones
comparable for one run. Use the per-site `timezone` key to set a zone for
good.

### Secrets, and unknown keys

Any string value in the config may be written as `${VAR}` and read from the
environment, or from a `.env` file beside the config. That keeps a key path,
or a property ID, out of a file you might paste into an issue. An unset or
empty variable is an error that names both the variable and the config key
that wanted it. The real environment wins over `.env`, so
`GA4_CREDENTIALS=… ga4-realtime doctor` is a one-off override that edits
nothing. `init` writes `.env.example` as the template.

An unknown key is a **warning**, not an error: the tool names the key,
suggests the nearest valid spelling, and carries on. A typo'd `intervall` that
silently did nothing is the outcome worth preventing.

Everything else is validated at load time, before the first network call: at
least one enabled site, unique usable names, an all-digits property ID, a
credentials file that exists and can be read, a non-empty conversions list,
numbers inside their bounds, and a timezone that `ZoneInfo` can resolve. Each
failure names the file to edit and the key inside it.

---

## What it records

Each poll sends one Realtime request per site over the last 10 minutes
(`poll_window`). That window is wider than the 5-minute default interval, so
the tool re-reads and corrects late-arriving events and skipped cycles. Each
request breaks the traffic down by `eventName`, `deviceCategory` and
`country`, for `screenPageViews` and `eventCount`.

Rows are keyed on `(site, minute, event, device, country)` and **upserted**.
Polling a minute again *corrects* the stored row instead of adding to it. The
upsert overwrites every metric column and never accumulates, so quitting and
restarting continues the same history and doubles nothing.

Everything lands in one SQLite file, in three tables:

| Table | What is in it |
|---|---|
| `realtime_minute` | one row per site, minute, event, device and country |
| `poll_log` | one row per poll: rows fetched, inserted, updated, and the error if it failed |
| `site_meta` | each site's property ID, display name and timezone, as the live view resolved them |

**The database is the interface.** There is no `export` command and no
machine-readable output flag. This is plain SQLite at a path the tool prints,
and

```bash
sqlite3 ga4_realtime.db "
  SELECT event_name, SUM(event_count) FROM realtime_minute
  WHERE site = 'mysite' GROUP BY 1 ORDER BY 2 DESC"
```

needs nothing from this tool. Minute keys are fixed-width UTC strings, so
plain string comparison orders and ranges them without date functions.

The tool refuses a database written by a version from before sites existed,
and names the file, rather than failing later on a missing column.

---

## How it behaves

- Day boundaries use the **property's** timezone, never the machine's. The
  tool fetches it from the Admin API at startup and caches it in the database.
  The per-site `timezone` key overrides it permanently, and `--timezone`
  overrides it for one run, for every site. Two sites in different zones roll
  over at different moments, so `Tab` can move from a site at 23:40 to one
  already at 04:40 the next day. The header's timezone and clock make that
  legible.
- `report --site all`, and the piped form, print one section per site scoped
  to **that site's** local day. When the run covers more than one site, they
  add a combined total labelled *"sum of each site's local day"*, with a
  warning line naming the zones when they differ.
- Startup primes every site at once, so it costs one round-trip rather than
  N, and reports every credential, permission and timezone failure while
  stdout is still an ordinary terminal. A site that fails starts with a `✗` in
  the status strip and the run continues. The tool exits without opening the
  display only if every site fails.
- An API error during a run backs off exponentially with jitter and is logged.
  The dashboard keeps rendering, and the site's marker says what happened.
- Logs go to a rotating file beside the database, never to stdout. `--verbose`
  adds a log panel that carries every site's lines with its name.
- `--ascii` drops to plain ASCII everywhere. The tool turns it on by itself
  when the console encoding cannot represent block characters. It respects
  `NO_COLOR`.

---

## Limits

**Collection stops when the dashboard stops.** The process that draws is the
process that polls, so closing the terminal ends collection. This is an
accepted limitation of this version. A headless collector for Task Scheduler
or systemd is the obvious next step, and the collection layer depends on
nothing in the display, so it can be reused whole.

**There is no user count, of any kind.** The same visitor appears in
consecutive minute windows, and the Realtime API exposes no identifier to
deduplicate on, so `activeUsers` cannot be summed across minutes. In the
breakdown above, one visitor who fires `page_view`, `scroll` and
`session_start` also appears in three rows of the *same* minute, so it cannot
be summed across rows either. Google rejects the query outright: asking for
`activeUsers` alongside `eventName` returns *"Selected dimensions and metrics
cannot be queried together"*. So the table has no `active_users` column, and
there is nothing to sum by mistake.

**There are no page paths.** The Realtime API has no `pagePath` dimension. Its
only page dimension is `unifiedScreenName`, which on web returns the page
*title*. The breakdown uses `eventName` instead, which also puts a site's
conversion events on screen live.

There is no web UI, no HTTP server, no hosted component, and no alerting.
Alerting waits until there is enough collected history to calibrate thresholds
against real numbers.

---

## Contributing

Bug reports and patches are welcome. [CONTRIBUTING.md](CONTRIBUTING.md) has
the mechanics: how to install the development extras, run `pytest`, run
`ruff`, and what a pull request should look like.

To find out why something is shaped as it is, read the code. Most comments in
this codebase record a decision already tried and rejected. There is no
separate design document, because it would be the copy that drifts.

## Licence

MIT. See [LICENSE](LICENSE). Version history is in
[CHANGELOG.md](CHANGELOG.md).
