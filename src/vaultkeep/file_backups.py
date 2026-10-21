"""Select independent file backups and their stable destination namespaces."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from contextlib import suppress
from pathlib import Path, PurePosixPath

from vaultkeep.config.models import JobConfig, SourceConfig
from vaultkeep.errors import DestinationError, SourceDiscoveryError
from vaultkeep.sources.exclusions import compile_exclusions

_FILE_DIRECTORY = re.compile(r"file-[a-z0-9-]{1,40}-[0-9a-f]{64}")


def file_key(name: str) -> str:
    """Keep names readable, bounded, and distinct on case-insensitive shares."""
    label = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "file"
    digest = hashlib.sha256(os.fsencode(name)).hexdigest()
    return f"file-{label}-{digest}"


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


def file_config(config: JobConfig, path: Path) -> JobConfig:
    """Derive one file's source and destination without changing its job ID."""
    source = config.sources[0]
    prefix = (
        str(PurePosixPath(source.archive_prefix or "", path.name))
        if source.archive_path_mode == "prefix"
        else None
    )
    child = destination_config(config, Path(config.destination.root) / file_key(path.name))
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
    try:
        children = sorted(root.iterdir())
    except OSError as error:
        raise DestinationError(f"Cannot list per-file destinations {root}: {error}") from error
    destinations: list[JobConfig] = []
    for child in children:
        if _FILE_DIRECTORY.fullmatch(child.name) is not None:
            require_file_directory(child)
            destinations.append(destination_config(config, child))
    return tuple(destinations)
