"""The one exception type the user is ever meant to read."""


class ConfigError(Exception):
    """A misconfiguration the user can fix, reported without a traceback.

    Raised for anything the user can act on -- a missing config file, a
    measurement ID where a property ID belongs, an unreadable key, a database
    written by an older schema. ``cli.main()`` catches it, prints the message
    alone and exits 1. A raw ``sqlite3`` or ``google.api_core`` exception
    reaching the user is therefore a bug, not a diagnostic: the message here
    is expected to name the file to edit and the exact fix.
    """
