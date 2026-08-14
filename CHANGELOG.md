# Changelog

All notable changes to this project are recorded in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

<!-- Section headings under a release, in this order and no others:
     Added, Changed, Deprecated, Removed, Fixed, Security.
     Unreleased stays here permanently and is emptied into a new dated
     section at each tag. -->

## [Unreleased]

### Added

- `--screenshot`, which draws one frame of the dashboard from invented data and
  exits. Three made-up sites in three timezones, one made-up day, and no API
  call, config file, database or credentials anywhere in the path, so it runs
  in a fresh clone on a machine that has never run `init` — which is what makes
  it usable for documentation, and for seeing what the tool looks like before
  deciding to set it up. The frame is drawn one row short of the terminal so
  the returning shell prompt does not scroll the header off the screen being
  photographed, and a reminder that every number is fabricated goes to stderr
  so `--screenshot > frame.txt` captures the frame alone. It honours `--site`,
  `--window`, `--ascii`, `--verbose` and `--timezone`, and exits 2 beside a
  subcommand word rather than ignoring one half of the line.

## [0.1.0] - 2026-08-14

First release. The tool was extracted on 2026-08-13 from the `tools/`
directory of the author's private `fchamone.com.br` repository, where it was a
single `ga4_realtime.py` script run through a Windows-only
`#!.venv\Scripts\python.exe` shebang. That repository keeps the pre-split file
verbatim; everything below is what turned it into an installable, multi-site
tool.

### Added

- An installable package: `src/` layout under `src/ga4_realtime/`, PEP 621
  metadata in `pyproject.toml`, and a `ga4-realtime` console script that works
  identically on Linux, macOS and Windows from any working directory, with no
  virtualenv activation. `python -m ga4_realtime` is equivalent.
- Multi-site support. Any number of GA4 properties are polled at once, one
  thread each, into one database; the dashboard shows one site at a time and
  `Tab`, `n`, `p` and `1`–`9` move between them. A status strip carries one
  marker per site so a site nobody is looking at cannot fail quietly, and the
  `--verbose` log panel is site-prefixed.
- A TOML configuration file, looked for in three places in order: `--config`,
  `./ga4-realtime.toml`, then the platform config directory. `[defaults]` is
  inherited by every `[[sites]]` block, which may override it; any string value
  may be written `${VAR}` and read from the environment or from a `.env` file
  beside the config. A command-line flag overrides `[defaults]` and every
  per-site value, for every site. Unknown keys warn and suggest the nearest
  valid spelling; everything else is validated before the first network call.
- Subcommands. `init` writes a starter config and `.env.example` and refuses to
  overwrite either; `doctor` checks config, credentials, both APIs and the
  timezone per site, and prints the resolved database and log paths without
  writing anything; `sites` lists every site as enabled, disabled or orphaned;
  `report` prints a day's summary per site plus a combined total; the bare
  invocation is the live dashboard. Every global flag — `--site`, `--db`,
  `--config` and the rest — is accepted on either side of the subcommand word,
  so `ga4-realtime --site clientb report` and `ga4-realtime report --site
  clientb` are the same line and the last spelling wins.
- Per-site credentials, loaded explicitly and passed to both Google clients.
  `os.environ["GOOGLE_APPLICATION_CREDENTIALS"]` is no longer written, so two
  sites with two service accounts can no longer end up sharing whichever key
  was assigned last. Clients are cached per resolved credentials path, so sites
  sharing one key share one pair of clients.
- Configurable conversion events per site, replacing the single hardcoded
  `generate_lead`, with conversion minutes marked along the chart baseline.
- A test suite (`pytest`), linting and formatting (`ruff`), and CI across
  `{ubuntu-latest, windows-latest}` × `{3.11, 3.12, 3.13}`. `tests/conftest.py`
  makes any test that constructs a Google API client fail, so the suite needs
  no credentials and no network.
- `CONTRIBUTING.md`, `LICENSE` (MIT), `CHANGELOG.md` and a fully documented
  `ga4-realtime.example.toml`.

### Changed

- A fresh, site-scoped schema. `site` leads the primary key of
  `realtime_minute` and `poll_log`, and the single-property key/value `meta`
  table is replaced by a typed `site_meta` row per site. The schema version is
  stamped in `PRAGMA user_version`, and a database written before sites existed
  is refused in words rather than failing later on a missing column.
- Paths resolve from the config file's directory, or from the platform config
  and data directories — no longer from `__file__`, which now points into
  `site-packages`. With `[storage] database` unset the database goes to the
  platform data directory (`%LOCALAPPDATA%` on Windows, not the roamed
  `%APPDATA%`), and the rotating log always lives beside the database.
- `report` reads only the local SQLite and works from `--db` alone: no config
  file, no credentials, no network.
- Startup primes every site concurrently, so it costs one round-trip rather
  than one per site, and a site that fails to prime starts with a failure
  marker instead of stopping the run.

### Removed

- `ga4_realtime.py` at the repository root, and its
  `#!.venv\Scripts\python.exe` shebang. No shim: run from the repo root it
  would shadow the installed package.
- `requirements.txt`. `pyproject.toml` owns the runtime dependencies with lower
  bounds, and the full freeze survives as `constraints.txt` — opt-in, and
  deliberately not applied by CI.
- The `export` command. The database is the interface: it is plain SQLite at a
  path the tool prints, in three documented tables.

[Unreleased]: https://github.com/fchamone/ga4_realtime/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/fchamone/ga4_realtime/releases/tag/v0.1.0
