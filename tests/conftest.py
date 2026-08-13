"""Fixtures shared by the whole suite.

For now it holds exactly one thing, and it is the one that has to be here
before any other test is written: **no test may reach Google.** Both API
clients are replaced, for every test, by something that raises when it is
constructed.

The alternative is worse than it looks. A test that quietly builds a real
client does not fail -- it authenticates from whatever credentials the machine
happens to have, makes a real request, and passes. On CI, with no credentials,
the same test fails with an auth error that reads like a broken build rather
than like a test that should never have been talking to a network at all.
Failing loudly at construction turns both cases into the same, obvious
message.
"""

import importlib

import pytest

# Not a network error and not a ConfigError: neither is true, and either would
# invite a test to catch it and carry on. A RuntimeError naming the client and
# the rule is what a reader of the failure needs.
_GUARD_MESSAGE = (
    "{name} was constructed inside a test. No test may reach the Google "
    "Analytics API -- pass a fake client in, or monkeypatch the function "
    "under test. See tests/conftest.py."
)

# Where each client name has to be blocked. The library module is the origin,
# but patching it alone is not enough: a module doing
# `from google.analytics.data_v1beta import BetaAnalyticsDataClient` at import
# time binds the real class into its own namespace, and that binding is what
# the code under test actually calls. So every module that imports a client by
# name is listed here too.
_DATA_CLIENT_MODULES = (
    "google.analytics.data_v1beta",
    # Lands in a later task; skipped until it exists.
    "ga4_realtime.ga4",
)
_ADMIN_CLIENT_MODULES = (
    "google.analytics.admin",
    "ga4_realtime.ga4",
)


def _raiser(name: str):
    """Build a stand-in that refuses to be constructed."""

    def _refuse(*args, **kwargs):
        raise RuntimeError(_GUARD_MESSAGE.format(name=name))

    return _refuse


def _block(monkeypatch, attr: str, module_names: tuple[str, ...]) -> None:
    for module_name in module_names:
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            # A module in the list that does not exist yet is not a reason to
            # fail collection of a suite that cannot be calling it either.
            continue
        if not hasattr(module, attr):
            continue
        monkeypatch.setattr(module, attr, _raiser(attr))


@pytest.fixture(autouse=True)
def no_data_api_client(monkeypatch):
    """Make constructing the Realtime API client raise."""
    _block(monkeypatch, "BetaAnalyticsDataClient", _DATA_CLIENT_MODULES)


@pytest.fixture(autouse=True)
def no_admin_api_client(monkeypatch):
    """Make constructing the Admin API client raise.

    Separate from the Data API fixture rather than folded into one, so a
    failure names which of the two APIs the test reached for.
    """
    _block(monkeypatch, "AnalyticsAdminServiceClient", _ADMIN_CLIENT_MODULES)
