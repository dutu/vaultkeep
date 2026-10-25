"""Strict discovery of committed backups without touching unrelated entries."""

from __future__ import annotations

import re
import stat
import string
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from os import stat_result
from pathlib import Path

from vaultkeep.archive.checksums import read_checksum_sidecar, verify_checksum_sidecar
from vaultkeep.config.models import JobConfig
from vaultkeep.destination.damage import DAMAGE_MARKER_NAME, read_damage_marker
from vaultkeep.destination.manifest import BackupManifest, load_manifest
from vaultkeep.destination.templates import STAGING_DIRECTORY_PREFIX
from vaultkeep.errors import DestinationDiscoveryError, DestinationError, ManifestError
from vaultkeep.progress import VerificationProgress, report_progress
from vaultkeep.state.models import BackupStateRecord

_FORMATTER = string.Formatter()
_BACKUP_ID = r"[0-9a-f]{32}"
_COLLISION_NUMBER = r"(?:[2-9]|[1-9][0-9]+)"


@dataclass(frozen=True, slots=True)
class MalformedDestinationEntry:
    """A template-matching entry that cannot participate in retention."""

    path: Path
    reason: str


@dataclass(frozen=True, slots=True)
class DiscoveredBackup:
    """A backup with validated metadata and optionally verified archive content."""

    directory: Path
    archive_path: Path
    checksum_path: Path
    manifest_path: Path
    base_name: str
    manifest: BackupManifest
    archive_size: int
    damage_reason: str | None = None
    replacement_backup_id: str | None = None

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
        return tuple(backup.state_record for backup in self.backups if backup.damage_reason is None)


def discover_backups(
    config: JobConfig,
    *,
    runtime_params: Mapping[str, str] | None = None,
    verify_archives: bool = True,
    metadata_only: bool = False,
    on_progress: Callable[[VerificationProgress], None] | None = None,
) -> DiscoveryResult:
    """Discover matching backups; metadata_only skips all checksum/content reads.

    Inventory includes both supported formats, using each stored manifest's facts.
    Other callers retain strict format and checksum validation.
    """
    root = Path(config.destination.root)
    formats = ("tar.zst", "tar.7z") if metadata_only else (config.archive.format,)
    base_matcher = re.compile(
        "(?:"
        + "|".join(
            _base_name_matcher(
                config.destination.name_template,
                config.job.id,
                archive_format,
                runtime_params=runtime_params,
            ).pattern
            for archive_format in formats
        )
        + ")"
    )
    matcher = re.compile(rf"(?:{base_matcher.pattern})(?:-(?:{_BACKUP_ID}|{_COLLISION_NUMBER}))?")
    try:
        children = tuple(root.iterdir())
    except OSError as error:
        raise DestinationDiscoveryError(f"Cannot list destination root {root}: {error}") from error

    backups: list[DiscoveredBackup] = []
    malformed: list[MalformedDestinationEntry] = []
    candidates = tuple(
        child
        for child in children
        if not child.name.startswith((".", STAGING_DIRECTORY_PREFIX))
        and matcher.fullmatch(child.name) is not None
    )
    report_progress(
        on_progress,
        VerificationProgress(
            "discovery",
            "Backup discovery",
            "ok",
            root,
            total=len(candidates),
            candidates=candidates,
        ),
    )
    for index, child in enumerate(candidates, start=1):
        operation = "Manifest/directory validation"
        current_path: Path | None = child
        created_at: str | None = None
        backup_id: str | None = None

        def report(event: VerificationProgress, *, index: int = index, child: Path = child) -> None:
            nonlocal operation, current_path, created_at, backup_id
            operation = event.operation
            current_path = event.path
            created_at = event.created_at or created_at
            backup_id = event.backup_id or backup_id
            report_progress(
                on_progress,
                replace(
                    event,
                    index=index,
                    total=len(candidates),
                    backup_directory=child,
                    created_at=created_at,
                    backup_id=backup_id,
                ),
            )

        report(VerificationProgress("backup", operation, path=child))
        try:
            backups.append(
                _validate_backup_directory(
                    child,
                    base_matcher,
                    config,
                    verify_archives=verify_archives,
                    metadata_only=metadata_only,
                    runtime_params=runtime_params,
                    on_progress=report if on_progress is not None else None,
                )
            )
        except (DestinationDiscoveryError, ManifestError) as error:
            malformed.append(MalformedDestinationEntry(child, str(error)))
            report(
                VerificationProgress("backup", operation, "failed", current_path, error=str(error))
            )
    return DiscoveryResult(tuple(backups), tuple(malformed))


def _validate_backup_directory(
    path: Path,
    base_matcher: re.Pattern[str],
    config: JobConfig,
    *,
    verify_archives: bool,
    metadata_only: bool = False,
    runtime_params: Mapping[str, str] | None = None,
    on_progress: Callable[[VerificationProgress], None] | None = None,
) -> DiscoveredBackup:
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
    try:
        manifests = tuple(child for child in path.iterdir() if child.suffix == ".json")
    except OSError as error:
        raise DestinationDiscoveryError(
            f"Cannot inspect backup directory {path}: {error}"
        ) from error
    if not manifests:
        raise DestinationDiscoveryError(f"Missing manifest: {path}")
    if len(manifests) != 1:
        raise DestinationDiscoveryError(
            f"Backup directory must contain exactly one manifest: {path}"
        )
    manifest_path = manifests[0]
    _require_regular_file(manifest_path)
    base_name = manifest_path.stem
    if base_matcher.fullmatch(base_name) is None:
        raise DestinationDiscoveryError(
            f"Manifest name does not match destination template: {path}"
        )
    manifest = load_manifest(manifest_path)
    if metadata_only:
        stored_matcher = _base_name_matcher(
            config.destination.name_template,
            config.job.id,
            manifest.archive_format,
            runtime_params=runtime_params,
        )
        if stored_matcher.fullmatch(base_name) is None:
            raise DestinationDiscoveryError(
                f"Manifest name does not match stored archive format: {path}"
            )
    suffix = path.name.removeprefix(base_name)
    if re.fullmatch(rf"-{_BACKUP_ID}", suffix):
        valid_name = suffix == f"-{manifest.backup_id}"
    else:
        valid_name = not suffix or re.fullmatch(rf"-{_COLLISION_NUMBER}", suffix) is not None
    if not path.name.startswith(base_name) or not valid_name or manifest.job != config.job.id:
        raise DestinationDiscoveryError(f"Manifest identity does not match directory name: {path}")
    if not metadata_only and manifest.archive_format != config.archive.format:
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
    archive_status = _require_regular_file(archive_path)
    _require_regular_file(checksum_path)
    _require_regular_file(manifest_path)
    try:
        damage = read_damage_marker(path)
    except DestinationError as error:
        raise DestinationDiscoveryError(str(error)) from error
    artifacts = {archive_path.name, checksum_path.name, manifest_path.name}
    if damage is not None:
        artifacts.add(DAMAGE_MARKER_NAME)
    _require_exact_artifacts(path, artifacts)
    backup = DiscoveredBackup(
        path,
        archive_path,
        checksum_path,
        manifest_path,
        base_name,
        manifest,
        archive_status.st_size,
        damage.reason if damage else None,
        damage.replacement_backup_id if damage else None,
    )
    if metadata_only:
        return backup
    report_progress(
        on_progress,
        VerificationProgress(
            "backup",
            "Manifest/directory validation",
            "ok",
            path,
            created_at=manifest.created_at,
            backup_id=manifest.backup_id,
        ),
    )
    operation = "SHA-256" if verify_archives else "Checksum metadata"
    if damage is not None:
        report_progress(
            on_progress,
            VerificationProgress(
                "backup",
                operation,
                "failed",
                archive_path,
                error=f"Marked damaged: {damage.reason}",
            ),
        )
        return backup
    report_progress(on_progress, VerificationProgress("backup", operation, path=archive_path))
    try:
        digest = (
            verify_checksum_sidecar(archive_path, checksum_path, on_progress=on_progress)
            if verify_archives
            else read_checksum_sidecar(archive_path, checksum_path)
        )
    except Exception as error:  # Archive errors are malformed destination facts here.
        raise DestinationDiscoveryError(f"Invalid archive checksum in {path}: {error}") from error
    report_progress(
        on_progress,
        VerificationProgress(
            "backup",
            operation,
            path=archive_path,
            detail=f"Manifest SHA-256: {manifest.archive_digest}; sidecar SHA-256: sha256:{digest}",
        ),
    )
    if manifest.archive_digest != f"sha256:{digest}":
        raise DestinationDiscoveryError(f"Manifest archive digest does not match checksum: {path}")
    report_progress(
        on_progress,
        VerificationProgress(
            "backup",
            operation,
            "ok",
            archive_path,
            detail=f"Manifest and sidecar SHA-256: {digest}",
        ),
    )
    return backup


def _require_regular_file(path: Path) -> stat_result:
    try:
        status = path.lstat()
    except FileNotFoundError as error:
        raise DestinationDiscoveryError(f"Required backup artifact is missing: {path}") from error
    except OSError as error:
        raise DestinationDiscoveryError(
            f"Cannot inspect backup artifact {path}: {error}"
        ) from error
    if not stat.S_ISREG(status.st_mode):
        raise DestinationDiscoveryError(f"Backup artifact is not a regular file: {path}")
    return status


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


def _base_name_matcher(
    template: str,
    job_id: str,
    archive_format: str,
    *,
    runtime_params: Mapping[str, str] | None = None,
) -> re.Pattern[str]:
    """Build a conservative matcher for the configured backup base-name template."""
    fragments: list[str] = []
    parameters = dict(runtime_params or {})
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
        if field in parameters:
            if format_spec:
                raise DestinationDiscoveryError(
                    f"Runtime destination template field {field!r} cannot use a format specifier"
                )
            fragments.append(re.escape(parameters[field]))
        elif field == "job":
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
    return re.compile("".join(fragments))


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
