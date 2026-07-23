"""Source discovery and exclusion APIs."""

from vaultkeep.sources.discovery import discover_sources
from vaultkeep.sources.entries import SourceEntry, SourceEntryType, SourceSnapshot
from vaultkeep.sources.hashing import calculate_config_fingerprint, calculate_source_digest

__all__ = [
    "SourceEntry",
    "SourceEntryType",
    "SourceSnapshot",
    "calculate_config_fingerprint",
    "calculate_source_digest",
    "discover_sources",
]
