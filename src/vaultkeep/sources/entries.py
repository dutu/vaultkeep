"""Immutable source discovery models."""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class SourceEntryType(StrEnum):
    """Supported v1 filesystem entry types."""

    FILE = "file"
    DIRECTORY = "directory"
    SYMLINK = "symlink"


@dataclass(frozen=True, slots=True)
class SourceEntry:
    """One immutable discovery result used by hashing and archiving."""

    absolute_path: Path
    archive_path: str
    entry_type: SourceEntryType
    mode: int
    uid: int
    gid: int
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int
    link_target: str | None
    followed_symlink: bool
    source_index: int

    @property
    def raw_archive_path(self) -> bytes:
        """Return the lossless filesystem encoding used for deterministic sorting."""
        return os.fsencode(self.archive_path)


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    """Deterministically sorted, duplicate-free source entries."""

    entries: tuple[SourceEntry, ...]

    def __post_init__(self) -> None:
        keys = tuple(entry.raw_archive_path for entry in self.entries)
        if keys != tuple(sorted(keys)):
            raise ValueError("SourceSnapshot entries must be sorted by raw archive path")
        if len(keys) != len(set(keys)):
            raise ValueError("SourceSnapshot archive paths must be unique")
