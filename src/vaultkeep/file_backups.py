"""Select independent file backups and their stable destination namespaces."""

from __future__ import annotations

import json
import os
import re
import stat
from contextlib import nullcontext, suppress
from pathlib import Path, PurePosixPath

from vaultkeep.config.models import JobConfig, SourceConfig
from vaultkeep.errors import DestinationError, SourceDiscoveryError, StateError
from vaultkeep.locking import destination_lock
from vaultkeep.sources.exclusions import compile_exclusions
from vaultkeep.state.atomic import write_json

FILE_INDEX_NAME = ".vaultkeep-files.json"
_DIRECTORY_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,59}")
_RESERVED_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def file_key(name: str) -> str:
    """Return the bounded readable base for a file's destination directory."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "file"


def _unique_entries(pairs: list[tuple[str, str]]) -> dict[str, str]:
    entries: dict[str, str] = {}
    for key, value in pairs:
        if key in entries:
            raise ValueError(f"Duplicate directory assignment: {key}")
        entries[key] = value
    return entries


def read_file_index(root: Path) -> dict[str, str]:
    """Read and validate assignments without following links or modifying storage."""
    path = root / FILE_INDEX_NAME
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return {}
    except OSError as error:
        raise DestinationError(f"Cannot read per-file index {path}: {error}") from error
    try:
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("Index must be a regular file with one link")
            entries = json.load(stream, object_pairs_hook=_unique_entries)
        if not isinstance(entries, dict):
            raise ValueError("Index must map directory names to source filenames")
        for key, name in entries.items():
            if (
                not _DIRECTORY_NAME.fullmatch(key)
                or key in _RESERVED_NAMES
                or not isinstance(name, str)
                or not name
                or name in (".", "..")
                or "/" in name
                or "\0" in name
            ):
                raise ValueError("Invalid directory or source filename")
        if len(set(entries.values())) != len(entries):
            raise ValueError("A source filename has multiple directory assignments")
        return entries
    except (OSError, ValueError, UnicodeError) as error:
        raise DestinationError(f"Invalid per-file index {path}: {error}") from error


def file_directory(root: Path, name: str, *, create: bool = False) -> str:
    """Resolve a permanent assignment, optionally reserving it under the parent lock."""
    with destination_lock(root) if create else nullcontext():
        entries = read_file_index(root)
        key = next((key for key, original in entries.items() if original == name), None)
        if key is None:
            if not create:
                raise DestinationError(f"No per-file index assignment for {name!r} in {root}")
            occupied = {path.name.casefold() for path in root.iterdir()} | set(entries)
            base = file_key(name)
            key = base
            suffix = 2
            while key in occupied or key in _RESERVED_NAMES:
                key = f"{base}-{suffix}"
                suffix += 1
            entries[key] = name
            try:
                write_json(root / FILE_INDEX_NAME, entries)
            except StateError as error:
                raise DestinationError(f"Cannot save per-file index: {error}") from error
        return key


def select_files(config: JobConfig) -> tuple[Path, ...]:
    """Select only immediate regular files, applying existing exclusion rules."""
    source = config.sources[0]
    root = Path(source.path)
    matcher = compile_exclusions((*config.exclude, *source.exclude))
    try:
        if not stat.S_ISDIR(root.lstat().st_mode):
            raise SourceDiscoveryError(f"per_file source must be a real directory: {root}")
        with os.scandir(root) as directory:
            return tuple(
                sorted(
                    (
                        Path(entry.path)
                        for entry in directory
                        if entry.is_file(follow_symlinks=False)
                        and not matcher.matches(entry.name, is_directory=False)
                    ),
                    key=lambda path: os.fsencode(path.name),
                )
            )
    except FileNotFoundError as error:
        if config.source_options.ignore_missing and not os.path.lexists(root):
            return ()
        raise SourceDiscoveryError(f"Cannot inspect per_file source {root}: {error}") from error
    except OSError as error:
        raise SourceDiscoveryError(f"Cannot inspect per_file source {root}: {error}") from error


def file_config(config: JobConfig, path: Path, *, create: bool = False) -> JobConfig:
    """Derive one file's source and destination without changing its job ID."""
    source = config.sources[0]
    prefix = (
        str(PurePosixPath(source.archive_prefix or "", path.name))
        if source.archive_path_mode == "prefix"
        else None
    )
    root = Path(config.destination.root)
    child = destination_config(config, root / file_directory(root, path.name, create=create))
    return child.model_copy(
        update={
            "sources": [
                SourceConfig(
                    path=str(path),
                    archive_path_mode=source.archive_path_mode,
                    archive_prefix=prefix,
                    exclude=source.exclude,
                )
            ]
        }
    )


def destination_config(config: JobConfig, root: Path) -> JobConfig:
    """The destination marker belongs to the parent, checked by the caller."""
    return config.model_copy(
        update={
            "destination": config.destination.model_copy(
                update={"root": str(root), "marker_file": None}
            )
        }
    )


def require_file_directory(root: Path, *, create: bool = False) -> None:
    """Refuse links and non-directories in the managed namespace."""
    try:
        if create:
            with suppress(FileExistsError):
                root.mkdir(mode=0o700)
        if not stat.S_ISDIR(root.lstat().st_mode):
            raise DestinationError(f"Per-file destination is not a real directory: {root}")
    except OSError as error:
        raise DestinationError(f"Cannot access per-file destination {root}: {error}") from error


def file_destinations(config: JobConfig) -> tuple[JobConfig, ...]:
    """Find histories even when their source files have been deleted or excluded."""
    root = Path(config.destination.root)
    return tuple(destination_config(config, root / key) for key in sorted(read_file_index(root)))
