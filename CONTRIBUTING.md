# Contributing

Bug reports, patches and questions are welcome.

This file is the mechanics only — how to install the project, run the tests,
run the linter, and what a pull request should look like. It is short on
purpose.

## Set up

Python 3.11, 3.12 or 3.13. Create a virtualenv beside the code and install the
package in editable mode with its development extras.

Windows (PowerShell):

```powershell
py -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

macOS and Linux:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
```

That is the whole install. `constraints.txt` is a full `pip freeze` of the
author's working set and is entirely opt-in — reach for it
(`pip install -e . -c constraints.txt`) only when an upstream release has
broken something and you want the set known to work while you find out what.

Activating the virtualenv is optional; every command below also works spelled
out as `.venv\Scripts\python.exe -m …` (Windows) or `.venv/bin/python -m …`.

## Tests

```bash
pytest                                  # the whole suite
pytest tests/test_store.py -q           # one file
pytest -k timezone -q                   # every test whose name matches
pytest --cov=ga4_realtime --cov-report=term-missing
```

`pytest` is configured in `pyproject.toml`, so it finds `tests/` from any
working directory inside the repo.

You do not need GA4 credentials, a config file or a network connection to run
the suite: `tests/conftest.py` makes any test that constructs a Google API
client fail with a message saying so.

## Lint and format

[ruff](https://docs.astral.sh/ruff/) does both jobs, configured in
`pyproject.toml` (line length 79).

```bash
ruff check src tests            # lint
ruff check --fix src tests      # lint, fixing what it safely can
ruff format src tests           # reformat in place
ruff format --check src tests   # what CI asks: formatted, no changes made
```

## Pull requests

- One topic per pull request. Two unrelated fixes are two pull requests.
- Run `ruff check src tests`, `ruff format --check src tests` and `pytest`
  before you push. CI runs exactly those three.
- New behaviour comes with a test; a bug fix comes with the test that fails
  without it.
- Add a line to `CHANGELOG.md` under `[Unreleased]`, in the section that fits
  (`Added`, `Changed`, `Deprecated`, `Removed`, `Fixed`, `Security`).
- Write commit messages in the imperative — "add a `--json` flag", not "added"
  — with a subject line under about 72 characters and the reasoning in the
  body.
- Never commit `.env` or anything under `secrets/` — both are gitignored, and
  what sits in `secrets/` is a private key. Read `git status` before every
  commit anyway; your own config file is not ignored for you.

CI runs `{ubuntu-latest, windows-latest}` × `{3.11, 3.12, 3.13}`. All six cells
have to be green before a pull request is merged. Windows is not optional: it
is where the tool is developed, and several code paths exist only for it.

## Reporting a bug

Open an issue at
<https://github.com/fchamone/ga4_realtime/issues> with:

- your operating system and Python version,
- the output of `ga4-realtime --version`,
- the exact command you ran and what it printed.

Redact freely, and never paste the contents of your service-account key file —
it is a private key, and an issue is a public place.
