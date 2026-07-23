"""Installed distribution version access."""

from importlib.metadata import version

_DISTRIBUTION_NAME = "vaultkeep"


def installed_version() -> str:
    """Return the installed Vaultkeep distribution version."""
    return version(_DISTRIBUTION_NAME)
