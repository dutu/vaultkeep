"""Deterministic filesystem source traversal."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from pathlib import Path, PurePosixPath

from vaultkeep.config.models import JobConfig
from vaultkeep.errors import SourceDiscoveryError
from vaultkeep.sources.entries import SourceEntry, SourceEntryType, SourceSnapshot
from vaultkeep.sources.exclusions import (
    ExclusionMatcher,
    InvalidExclusionPattern,
    compile_exclusions,
)

_InodeKey = tuple[int, int]


def discover_sources(config: JobConfig) -> SourceSnapshot:
    """Discover every selected source entry and return one immutable snapshot."""
    discovered: list[SourceEntry] = []
    options = config.source_options

    for source_index, source in enumerate(config.sources):
        root = Path(source.path)
        if not os.path.lexists(root):
            if options.ignore_missing:
                continue
            raise SourceDiscoveryError(f"Configured source does not exist: {root}")

        try:
            matcher = compile_exclusions((*config.exclude, *source.exclude))
        except InvalidExclusionPattern as error:
            raise SourceDiscoveryError(str(error)) from error
        try:
            followed_root = root.stat() if options.follow_symlinks else root.lstat()
            root_device = _device_id(root, followed_root)
            discovered.extend(
                _walk(
                    root=root,
                    path=root,
                    relative_path="",
                    source_index=source_index,
                    matcher=matcher,
                    follow_symlinks=options.follow_symlinks,
                    cross_filesystems=options.cross_filesystems,
                    root_device=root_device,
                    ancestors=frozenset(),
                    is_root=True,
                )
            )
        except OSError as error:
            raise SourceDiscoveryError(f"Cannot inspect source {root}: {error}") from error

    if not discovered:
        raise SourceDiscoveryError(
            "Exclusions or missing-source handling removed every configured source."
        )

    entries = tuple(sorted(discovered, key=lambda entry: entry.raw_archive_path))
    duplicate = _duplicate_archive_path(entries)
    if duplicate is not None:
        raise SourceDiscoveryError(f"Duplicate archive member path: {duplicate!r}")
    return SourceSnapshot(entries)


def _walk(
    *,
    root: Path,
    path: Path,
    relative_path: str,
    source_index: int,
    matcher: ExclusionMatcher,
    follow_symlinks: bool,
    cross_filesystems: bool,
    root_device: int,
    ancestors: frozenset[_InodeKey],
    is_root: bool,
) -> Iterator[SourceEntry]:
    link_stat = path.lstat()
    is_link = stat.S_ISLNK(link_stat.st_mode)
    effective_stat = path.stat() if is_link and follow_symlinks else link_stat
    entry_type = _entry_type(effective_stat.st_mode, is_link and not follow_symlinks, path)
    is_directory = entry_type is SourceEntryType.DIRECTORY

    exclusion_path = path.name if is_root and not is_directory else relative_path
    if not is_root and matcher.matches(exclusion_path, is_directory=is_directory):
        return
    if is_root and not is_directory and matcher.matches(exclusion_path, is_directory=is_directory):
        return

    link_target = os.readlink(path) if entry_type is SourceEntryType.SYMLINK else None
    yield _source_entry(
        path,
        source_index,
        entry_type,
        effective_stat,
        link_target,
        followed_symlink=is_link and follow_symlinks,
    )

    if not is_directory:
        return
    if not cross_filesystems and not is_root and _device_id(path, effective_stat) != root_device:
        return

    inode_key = (effective_stat.st_dev, effective_stat.st_ino)
    if inode_key in ancestors:
        raise SourceDiscoveryError(f"Directory cycle detected while following {path}")
    child_ancestors = ancestors | {inode_key}

    try:
        with os.scandir(path) as directory:
            children = sorted(directory, key=lambda item: os.fsencode(item.name))
    except OSError as error:
        raise SourceDiscoveryError(f"Cannot read source directory {path}: {error}") from error

    for child in children:
        child_path = Path(child.path)
        child_relative = child_path.relative_to(root).as_posix()
        yield from _walk(
            root=root,
            path=child_path,
            relative_path=child_relative,
            source_index=source_index,
            matcher=matcher,
            follow_symlinks=follow_symlinks,
            cross_filesystems=cross_filesystems,
            root_device=root_device,
            ancestors=child_ancestors,
            is_root=False,
        )


def _entry_type(mode: int, preserve_symlink: bool, path: Path) -> SourceEntryType:
    if preserve_symlink:
        return SourceEntryType.SYMLINK
    if stat.S_ISREG(mode):
        return SourceEntryType.FILE
    if stat.S_ISDIR(mode):
        return SourceEntryType.DIRECTORY
    raise SourceDiscoveryError(f"Unsupported source entry type: {path}")


def _source_entry(
    path: Path,
    source_index: int,
    entry_type: SourceEntryType,
    status: os.stat_result,
    link_target: str | None,
    *,
    followed_symlink: bool,
) -> SourceEntry:
    return SourceEntry(
        absolute_path=path,
        archive_path=_archive_path(path),
        entry_type=entry_type,
        mode=stat.S_IMODE(status.st_mode),
        uid=status.st_uid,
        gid=status.st_gid,
        device=status.st_dev,
        inode=status.st_ino,
        size=status.st_size,
        mtime_ns=status.st_mtime_ns,
        ctime_ns=status.st_ctime_ns,
        link_target=link_target,
        followed_symlink=followed_symlink,
        source_index=source_index,
    )


def _archive_path(path: Path) -> str:
    absolute = path.absolute()
    parts = absolute.parts[1:] if absolute.anchor else absolute.parts
    return PurePosixPath(*parts).as_posix()


def _device_id(path: Path, status: os.stat_result) -> int:
    del path
    return status.st_dev


def _duplicate_archive_path(entries: tuple[SourceEntry, ...]) -> str | None:
    previous: bytes | None = None
    for entry in entries:
        current = entry.raw_archive_path
        if current == previous:
            return entry.archive_path
        previous = current
    return None
