"""Record the GA4 Realtime window and render it as a live dashboard.

GA4's Realtime report only reaches back 30 minutes, and the standard reports
lag 4-8 hours behind. Whatever happens while nobody is watching the screen is
simply lost. This package polls that window on a schedule for one or more
properties, accumulates the result into a local SQLite file, and draws a
"today so far" view from the accumulated history -- numbers the GA4 UI will
not show for hours yet.
"""

from importlib.metadata import version

# Read back from the installed distribution metadata rather than written out
# here, so pyproject.toml stays the single place the version is spelled. The
# cost is that an uninstalled source tree has no version at all, which is the
# correct answer: with a src/ layout there is nothing to import from one.
__version__ = version("ga4-realtime")

__all__ = ["__version__"]
