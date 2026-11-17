"""Server settings read from the environment, with a default and bounds.

Read at call time, so a changed variable takes effect without a restart in
tests and in a worker that re-reads its environment. A value that is not a
number, or is out of bounds, never breaks a request: a bad number falls back
to the default, an out-of-range one is clamped to the bounds.
"""
import os


def integer(name, default, lo=None, hi=None):
    try:
        value = int(str(os.getenv(name, "")).strip() or default)
    except ValueError:
        value = default
    if lo is not None:
        value = max(lo, value)
    if hi is not None:
        value = min(hi, value)
    return value


def number(name, default, lo=None, hi=None):
    try:
        value = float(str(os.getenv(name, "")).strip() or default)
    except ValueError:
        value = default
    if lo is not None:
        value = max(lo, value)
    if hi is not None:
        value = min(hi, value)
    return value


def names(name, default, allowed=None):
    """A comma-separated list, lowercased; unknown entries (when `allowed` is
    given) are dropped; an empty or unset value is the default."""
    raw = os.getenv(name, "")
    items = [x.strip().lower() for x in raw.split(",") if x.strip()] if raw.strip() else list(default)
    if allowed is not None:
        items = [x for x in items if x in allowed]
    return list(dict.fromkeys(items)) or list(default)


def text(name, default):
    value = os.getenv(name, "")
    return value.strip() if value.strip() else default
