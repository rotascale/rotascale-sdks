"""The installed version, resolved once.

subhadipmitra@: Read from installed metadata rather than written down, because
it was written down in three places — `pyproject.toml`, `__init__.py`, and a
hardcoded user-agent string — and two of them were already stale at 0.1.0 while
PyPI carried 0.1.1. A user-agent that lies about its version makes a support
conversation start from a false premise.

`pyproject.toml` is now the only place the number appears.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("rotascale")
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0.0.0.dev0"

USER_AGENT = f"rotascale-python/{__version__}"
