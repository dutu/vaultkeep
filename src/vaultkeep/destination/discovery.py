"""Strict discovery of committed backups without touching unrelated entries."""

from __future__ import annotations

import re
import stat
import string
from dataclasses import dataclass
from pathlib import Path

from vaultkeep.archive.checksums import verify_checksum_sidecar
from vaultkeep.config.models import JobConfig
from vaultkeep.destination.manifest import BackupManifest, load_manifest
from vaultkeep.errors import DestinationDiscoveryError, ManifestError
from vaultkeep.state.models import BackupStateRecord

_FORMATTER = string.Formatter()
_BACKUP_ID = r"[0-9a-f]{32}"


@dataclass(frozen=True, slots=True)
class MalformedDestinationEntry:
    """A template-matching entry that cannot participate in retention."""

    path: Path
    reason: str


@dataclass(frozen=True, slots=True)
class DiscoveredBackup:
    """A complete backup whose identity and integrity have been validated."""

    directory: Path
    archive_path: Path
    checksum_path: Path
    manifest_path: Path
    base_name: str
    manifest: BackupManifest

    @property
    def state_record(self) -> BackupStateRecord:
        """Expose the validated subset used for local-state reconstruction."""
        return BackupStateRecord(
            job_id=self.manifest.job,
            backup_id=self.manifest.backup_id,
            created_at_utc=self.manifest.created_at_utc,
            source_digest=self.manifest.source_digest,
            config_fingerprint=self.manifest.config_fingerprint,
            backup_path=str(self.archive_path),
            application_version=self.manifest.application_version,
            encrypted=self.manifest.encrypted,
        )


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    """All classified immediate destination entries."""

    backups: tuple[DiscoveredBackup, ...]
    malformed: tuple[MalformedDestinationEntry, ...]

    @property
    def state_records(self) -> tuple[BackupStateRecord, ...]:
        return tuple(backup.state_record for backup in self.backups)


def discover_backups(config: JobConfig) -> DiscoveryResult:
    """Return valid configured-job backups and matching malformed entries."""
    root = Path(config.destination.root)
    matcher = _directory_matcher(
        config.destination.name_template, config.job.id, config.archive.format
    )
    try:
        children = tuple(root.iterdir())
    except OSError as error:
        raise DestinationDiscoveryError(f"Cannot list destination root {root}: {error}") from error

    backups: list[DiscoveredBackup] = []
    malformed: list[MalformedDestinationEntry] = []
    for child in children:
        if child.name.startswith("."):
            continue
        match = matcher.fullmatch(child.name)
        if match is None:
            continue
        try:
            backups.append(_validate_backup_directory(child, match.group("base"), config))
        except (DestinationDiscoveryError, ManifestError) as error:
            malformed.append(MalformedDestinationEntry(child, str(error)))
    return DiscoveryResult(tuple(backups), tuple(malformed))


def _validate_backup_directory(path: Path, base_name: str, config: JobConfig) -> DiscoveredBackup:
    try:
        directory_status = path.lstat()
    except OSError as error:
        raise DestinationDiscoveryError(
            f"Cannot inspect destination entry {path}: {error}"
        ) from error
    if not stat.S_ISDIR(directory_status.st_mode):
        raise DestinationDiscoveryError(f"Matching destination entry is not a directory: {path}")
    if path.is_symlink():
        raise DestinationDiscoveryError(f"Matching destination entry is a symbolic link: {path}")
    manifest_path = path / f"{base_name}.json"
    manifest = load_manifest(manifest_path)
    backup_id = path.name.removesuffix("").rsplit("-", 1)[-1]
    if manifest.job != config.job.id or manifest.backup_id != backup_id:
        raise DestinationDiscoveryError(f"Manifest identity does not match directory name: {path}")
    if manifest.archive_format != config.archive.format:
        raise DestinationDiscoveryError(
            f"Manifest archive format does not match job configuration: {path}"
        )
    expected_archive = f"{base_name}.{manifest.archive_format}"
    if manifest.archive != expected_archive:
        raise DestinationDiscoveryError(
            f"Manifest archive name does not match destination layout: {path}"
        )
    archive_path = path / expected_archive
    checksum_path = path / f"{expected_archive}.sha256"
    _require_regular_file(archive_path)
    _require_regular_file(checksum_path)
    _require_regular_file(manifest_path)
    _require_exact_artifacts(path, {archive_path.name, checksum_path.name, manifest_path.name})
    try:
        digest = verify_checksum_sidecar(archive_path, checksum_path)
    except Exception as error:  # Archive errors are malformed destination facts here.
        raise DestinationDiscoveryError(f"Invalid archive checksum in {path}: {error}") from error
    if manifest.archive_digest != f"sha256:{digest}":
        raise DestinationDiscoveryError(f"Manifest archive digest does not match checksum: {path}")
    return DiscoveredBackup(path, archive_path, checksum_path, manifest_path, base_name, manifest)


def _require_regular_file(path: Path) -> None:
    try:
        status = path.lstat()
    except OSError as error:
        raise DestinationDiscoveryError(f"Required backup artifact is missing: {path}") from error
    if not stat.S_ISREG(status.st_mode):
        raise DestinationDiscoveryError(f"Backup artifact is not a regular file: {path}")


def _require_exact_artifacts(directory: Path, expected: set[str]) -> None:
    try:
        actual = {child.name for child in directory.iterdir()}
    except OSError as error:
        raise DestinationDiscoveryError(
            f"Cannot inspect backup directory {directory}: {error}"
        ) from error
    if actual != expected:
        raise DestinationDiscoveryError(
            f"Backup directory has unexpected or missing artifacts: {directory}"
        )


def _directory_matcher(template: str, job_id: str, archive_format: str) -> re.Pattern[str]:
    """Build a conservative matcher for the configured name template and ID suffix."""
    fragments: list[str] = []
    try:
        parsed = tuple(_FORMATTER.parse(template))
    except ValueError as error:
        raise DestinationDiscoveryError(f"Invalid destination name template: {error}") from error
    for literal, field, format_spec, conversion in parsed:
        fragments.append(re.escape(literal))
        if field is None:
            continue
        if conversion is not None:
            raise DestinationDiscoveryError("Destination template conversions are not supported")
        if field == "job":
            fragments.append(re.escape(job_id))
        elif field == "format":
            fragments.append(re.escape(archive_format))
        elif field == "source_hash":
            precision = _source_hash_precision(format_spec or "")
            fragments.append(rf"[0-9a-f]{{{precision}}}")
        elif field in {"timestamp", "timestamp_utc"}:
            fragments.append(_timestamp_pattern(format_spec or ""))
        elif field == "hostname":
            fragments.append(r"[^/\\\x00]+?")
        else:
            raise DestinationDiscoveryError(f"Unsupported destination template field: {field}")
    return re.compile(rf"(?P<base>{''.join(fragments)})-(?P<backup_id>{_BACKUP_ID})")


def _source_hash_precision(format_spec: str) -> int:
    if not format_spec:
        return 64
    match = re.fullmatch(r"\.(\d+)", format_spec)
    if match is None or not 1 <= int(match.group(1)) <= 64:
        raise DestinationDiscoveryError(
            "source_hash format must be a precision from .1 through .64"
        )
    return int(match.group(1))


def _timestamp_pattern(format_spec: str) -> str:
    if not format_spec:
        return r"[^/\\\x00]+?"
    directives = {
        "%Y": r"\d{4}",
        "%m": r"\d{2}",
        "%d": r"\d{2}",
        "%H": r"\d{2}",
        "%M": r"\d{2}",
        "%S": r"\d{2}",
        "%f": r"\d{1,6}",
        "%z": r"(?:[+-]\d{4})",
        "%Z": r"[A-Za-z0-9_+.-]+",
        "%j": r"\d{3}",
        "%U": r"\d{2}",
        "%W": r"\d{2}",
        "%V": r"\d{2}",
        "%G": r"\d{4}",
        "%u": r"[1-7]",
        "%w": r"[0-6]",
        "%%": "%",
    }
    result = ""
    index = 0
    while index < len(format_spec):
        if format_spec[index] != "%":
            result += re.escape(format_spec[index])
            index += 1
            continue
        token = format_spec[index : index + 2]
        if token not in directives:
            raise DestinationDiscoveryError(f"Unsupported timestamp directive: {token}")
        result += directives[token]
        index += 2
    return result
