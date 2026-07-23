"""Versioned, deterministic source and configuration hashing."""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import stat
from collections.abc import Mapping
from typing import BinaryIO, Protocol

from vaultkeep.config.models import JobConfig
from vaultkeep.errors import SourceChangedError, SourceHashError
from vaultkeep.sources.entries import SourceEntry, SourceEntryType, SourceSnapshot

SOURCE_DIGEST_FORMAT_VERSION = 1
CONFIG_FINGERPRINT_FORMAT_VERSION = 1
_HASH_CHUNK_SIZE = 1024 * 1024
_METADATA_POLICY = ("mode", "uid", "gid")


class _Digest(Protocol):
    def update(self, data: bytes) -> object:
        """Add bytes to the digest."""


def calculate_source_digest(
    snapshot: SourceSnapshot,
    *,
    format_version: int = SOURCE_DIGEST_FORMAT_VERSION,
) -> str:
    """Hash one immutable source snapshot with collision-safe framing."""
    if format_version < 1:
        raise ValueError("Source digest format version must be positive")

    digest = hashlib.sha256()
    _field(digest, b"format", _unsigned(format_version))
    _field(digest, b"entry-count", _unsigned(len(snapshot.entries)))

    try:
        for entry in snapshot.entries:
            _verify_entry(entry)
            _field(digest, b"entry", b"")
            _field(digest, b"path", entry.raw_archive_path)
            _field(digest, b"type", entry.entry_type.value.encode("ascii"))
            _field(digest, b"mode", _unsigned(entry.mode))
            _field(digest, b"uid", _unsigned(entry.uid))
            _field(digest, b"gid", _unsigned(entry.gid))
            if entry.entry_type is SourceEntryType.FILE:
                _hash_file(digest, entry)
            elif entry.entry_type is SourceEntryType.SYMLINK:
                if entry.link_target is None:
                    raise SourceChangedError(
                        f"Symlink target is absent from snapshot: {entry.absolute_path}"
                    )
                _field(digest, b"link-target", os.fsencode(entry.link_target))

        for entry in snapshot.entries:
            _verify_entry(entry)
    except SourceHashError:
        raise
    except OSError as error:
        raise SourceHashError(f"Cannot hash source entry: {error}") from error

    return f"sha256:{digest.hexdigest()}"


def calculate_config_fingerprint(
    config: JobConfig,
    *,
    format_version: int = CONFIG_FINGERPRINT_FORMAT_VERSION,
) -> str:
    """Hash only configuration that changes backup identity or content."""
    if format_version < 1:
        raise ValueError("Configuration fingerprint format version must be positive")

    document: Mapping[str, object] = {
        "fingerprint_version": format_version,
        "config_version": config.config_version,
        "job": config.job.id,
        "sources": [
            {
                "path": _normalize_posix_path(source.path),
                "exclude": source.exclude,
            }
            for source in config.sources
        ],
        "exclude": config.exclude,
        "source_options": {
            "follow_symlinks": config.source_options.follow_symlinks,
            "cross_filesystems": config.source_options.cross_filesystems,
            "ignore_missing": config.source_options.ignore_missing,
        },
        "destination": {
            "root": _normalize_posix_path(config.destination.root),
            "name_template": config.destination.name_template,
            "marker_file": config.destination.marker_file,
            "require_mount": config.destination.require_mount,
        },
        "archive": {
            "format": config.archive.format,
            "compression_level": config.archive.compression_level,
        },
        "encryption": {
            "mode": config.encryption.mode,
            "password_file": (
                _normalize_posix_path(config.encryption.password_file)
                if config.encryption.password_file is not None
                else None
            ),
        },
        "metadata_policy": _METADATA_POLICY,
    }
    encoded = json.dumps(
        document,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _hash_file(digest: _Digest, entry: SourceEntry) -> None:
    try:
        with entry.absolute_path.open("rb", buffering=0) as stream:
            before = os.fstat(stream.fileno())
            _verify_status(entry, before, descriptor_status=True)
            _field_header(digest, b"content", entry.size)
            remaining = entry.size
            while remaining:
                chunk = _read_chunk(stream, min(_HASH_CHUNK_SIZE, remaining))
                if not chunk:
                    raise SourceChangedError(
                        f"File became shorter while hashing: {entry.absolute_path}"
                    )
                digest.update(chunk)
                remaining -= len(chunk)
            if _read_chunk(stream, 1):
                raise SourceChangedError(f"File became longer while hashing: {entry.absolute_path}")
            after = os.fstat(stream.fileno())
            _verify_status(entry, after, descriptor_status=True)
    except SourceHashError:
        raise
    except OSError as error:
        raise SourceHashError(f"Cannot read source file {entry.absolute_path}: {error}") from error


def _read_chunk(stream: BinaryIO, size: int) -> bytes:
    return stream.read(size)


def _verify_entry(entry: SourceEntry) -> None:
    try:
        status = (
            entry.absolute_path.stat() if entry.followed_symlink else entry.absolute_path.lstat()
        )
    except OSError as error:
        raise SourceChangedError(
            f"Source entry is no longer accessible: {entry.absolute_path}: {error}"
        ) from error
    _verify_status(entry, status)
    if entry.entry_type is SourceEntryType.SYMLINK:
        try:
            target = os.readlink(entry.absolute_path)
        except OSError as error:
            raise SourceChangedError(
                f"Cannot re-read symlink target {entry.absolute_path}: {error}"
            ) from error
        if target != entry.link_target:
            raise SourceChangedError(
                f"Symlink target changed after discovery: {entry.absolute_path}"
            )


def _verify_status(
    entry: SourceEntry,
    status: os.stat_result,
    *,
    descriptor_status: bool = False,
) -> None:
    expected_type = entry.entry_type
    actual_type = _status_type(status.st_mode, entry.followed_symlink)
    current = (
        actual_type,
        stat.S_IMODE(status.st_mode),
        status.st_uid,
        status.st_gid,
        status.st_dev,
        status.st_ino,
        status.st_size,
        status.st_mtime_ns,
    )
    expected = (
        expected_type,
        entry.mode,
        entry.uid,
        entry.gid,
        entry.device,
        entry.inode,
        entry.size,
        entry.mtime_ns,
    )
    # Windows reports different ctime semantics for path and descriptor stats.
    # Debian uses ctime as an additional consistency signal, never as digest input.
    ctime_matches = (os.name == "nt" and descriptor_status) or status.st_ctime_ns == entry.ctime_ns
    if current != expected or not ctime_matches:
        raise SourceChangedError(f"Source entry changed after discovery: {entry.absolute_path}")


def _status_type(mode: int, followed_symlink: bool) -> SourceEntryType | None:
    if stat.S_ISREG(mode):
        return SourceEntryType.FILE
    if stat.S_ISDIR(mode):
        return SourceEntryType.DIRECTORY
    if stat.S_ISLNK(mode) and not followed_symlink:
        return SourceEntryType.SYMLINK
    return None


def _field(digest: _Digest, tag: bytes, payload: bytes) -> None:
    _field_header(digest, tag, len(payload))
    digest.update(payload)


def _field_header(digest: _Digest, tag: bytes, payload_size: int) -> None:
    if len(tag) > 0xFFFF:
        raise ValueError("Digest field tag is too long")
    digest.update(len(tag).to_bytes(2, "big"))
    digest.update(tag)
    digest.update(payload_size.to_bytes(8, "big"))


def _unsigned(value: int) -> bytes:
    if value < 0:
        raise ValueError("Digest integer fields cannot be negative")
    return value.to_bytes(8, "big")


def _normalize_posix_path(value: str) -> str:
    normalized = posixpath.normpath(value)
    return "/" + normalized.lstrip("/") if value.startswith("/") else normalized
