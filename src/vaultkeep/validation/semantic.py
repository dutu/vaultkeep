"""Cross-field validation for configuration schema version 1."""

from __future__ import annotations

import posixpath
import re
import string
from datetime import datetime
from pathlib import Path, PurePosixPath

from vaultkeep.config.models import JobConfig
from vaultkeep.errors import ConfigurationError, IssuePathPart, ValidationIssue
from vaultkeep.sources.exclusions import InvalidExclusionPattern, compile_exclusions

_SUPPORTED_TEMPLATE_FIELDS = frozenset(
    {"job", "hostname", "timestamp", "timestamp_utc", "source_hash", "format"}
)
_TIMESTAMP_FIELDS = frozenset({"timestamp", "timestamp_utc"})
_WEEKDAYS = frozenset(
    {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"}
)
_TIME_PATTERN = re.compile(r"^(?P<hour>[01]\d|2[0-3]):(?P<minute>[0-5]\d)$")
_SOURCE_HASH_FORMAT = re.compile(r"^\.[1-9]\d*$")


def validate_semantics(config: JobConfig, *, config_path: Path | None = None) -> None:
    """Collect and raise all independently detectable semantic errors."""
    issues: list[ValidationIssue] = []
    _validate_paths(config, issues)
    _validate_exclusions(config, issues)
    _validate_template(config, issues)
    _validate_archive(config, issues)
    _validate_retention(config, issues)
    _validate_schedule(config, issues)
    _validate_hooks(config, issues)
    _validate_job_filename(config, config_path, issues)
    if issues:
        raise ConfigurationError(tuple(issues))


def _issue(
    issues: list[ValidationIssue],
    path: tuple[IssuePathPart, ...],
    message: str,
    code: str,
) -> None:
    issues.append(ValidationIssue(path, message, code))


def _validate_paths(config: JobConfig, issues: list[ValidationIssue]) -> None:
    normalized_sources: list[tuple[int, PurePosixPath]] = []
    for index, source in enumerate(config.sources):
        location = ("sources", index, "path")
        if "\0" in source.path:
            _issue(issues, location, "Source path contains a null character.", "path_null")
            continue
        normalized = _absolute_posix_path(source.path)
        if normalized is None:
            _issue(issues, location, "Source path must be absolute.", "path_absolute")
            continue
        normalized_sources.append((index, normalized))

    destination = config.destination.root
    normalized_destination: PurePosixPath | None = None
    if "\0" in destination:
        _issue(
            issues,
            ("destination", "root"),
            "Destination root contains a null character.",
            "path_null",
        )
    else:
        normalized_destination = _absolute_posix_path(destination)
        if normalized_destination is None:
            _issue(
                issues,
                ("destination", "root"),
                "Destination root must be absolute.",
                "path_absolute",
            )

    seen: dict[PurePosixPath, int] = {}
    for index, source_path in normalized_sources:
        previous = seen.get(source_path)
        if previous is not None:
            _issue(
                issues,
                ("sources", index, "path"),
                f"Source duplicates sources[{previous}].path after normalization.",
                "source_duplicate",
            )
        else:
            seen[source_path] = index

    for position, (index, source_path) in enumerate(normalized_sources):
        for other_index, other in normalized_sources[position + 1 :]:
            if source_path == other:
                continue
            if _is_within(source_path, other) or _is_within(other, source_path):
                _issue(
                    issues,
                    ("sources", other_index, "path"),
                    f"Source overlaps sources[{index}].path.",
                    "source_overlap",
                )

        if normalized_destination is not None and (
            _is_within(normalized_destination, source_path)
            or _is_within(source_path, normalized_destination)
        ):
            _issue(
                issues,
                ("destination", "root"),
                f"Destination and sources[{index}].path overlap.",
                "destination_source_overlap",
            )

    password_file = config.encryption.password_file
    if password_file is not None:
        if "\0" in password_file:
            _issue(
                issues,
                ("encryption", "password_file"),
                "Password-file path contains a null character.",
                "path_null",
            )
        elif _absolute_posix_path(password_file) is None:
            _issue(
                issues,
                ("encryption", "password_file"),
                "Password-file path must be absolute.",
                "path_absolute",
            )

    marker_file = config.destination.marker_file
    if marker_file is not None:
        marker_path = PurePosixPath(marker_file)
        if "\0" in marker_file or marker_path.is_absolute() or ".." in marker_path.parts:
            _issue(
                issues,
                ("destination", "marker_file"),
                "Marker file must be a relative path below the destination root.",
                "marker_path",
            )


def _absolute_posix_path(value: str) -> PurePosixPath | None:
    if not PurePosixPath(value).is_absolute():
        return None
    normalized = posixpath.normpath(value)
    return PurePosixPath("/" + normalized.lstrip("/"))


def _is_within(path: PurePosixPath, parent: PurePosixPath) -> bool:
    return path == parent or parent in path.parents


def _validate_exclusions(config: JobConfig, issues: list[ValidationIssue]) -> None:
    pattern_groups: list[tuple[tuple[IssuePathPart, ...], list[str]]] = [
        (("exclude",), config.exclude)
    ]
    pattern_groups.extend(
        (("sources", index, "exclude"), source.exclude)
        for index, source in enumerate(config.sources)
    )
    for base_path, patterns in pattern_groups:
        for index, pattern in enumerate(patterns):
            try:
                compile_exclusions((pattern,))
            except InvalidExclusionPattern as error:
                _issue(
                    issues,
                    (*base_path, index),
                    str(error),
                    "exclusion_pattern",
                )


def _validate_template(config: JobConfig, issues: list[ValidationIssue]) -> None:
    template = config.destination.name_template
    path = ("destination", "name_template")

    fields: list[str] = []
    has_invalid_field = False
    try:
        parsed = list(string.Formatter().parse(template))
    except ValueError as error:
        _issue(issues, path, f"Invalid template syntax: {error}", "template_syntax")
        return

    for _, field_name, format_spec, conversion in parsed:
        if field_name is None:
            continue
        format_spec = format_spec or ""
        fields.append(field_name)
        if field_name == "backup_id":
            has_invalid_field = True
            _issue(
                issues,
                path,
                "The {backup_id} placeholder is not allowed.",
                "template_backup_id",
            )
            continue
        if field_name not in _SUPPORTED_TEMPLATE_FIELDS:
            has_invalid_field = True
            _issue(
                issues,
                path,
                f"Unknown placeholder: {{{field_name}}}.",
                "template_placeholder",
            )
            continue
        if conversion is not None:
            has_invalid_field = True
            _issue(
                issues,
                path,
                f"Conversions are not supported for {{{field_name}}}.",
                "template_conversion",
            )
        if "{" in format_spec or "}" in format_spec:
            has_invalid_field = True
            _issue(
                issues,
                path,
                f"Nested fields are not supported for {{{field_name}}}.",
                "template_nested",
            )
        elif field_name == "source_hash":
            if format_spec and _SOURCE_HASH_FORMAT.fullmatch(format_spec) is None:
                has_invalid_field = True
                _issue(
                    issues,
                    path,
                    "source_hash accepts only precision truncation such as {source_hash:.12}.",
                    "template_format",
                )
        elif field_name not in _TIMESTAMP_FIELDS and format_spec:
            has_invalid_field = True
            _issue(
                issues,
                path,
                f"Format specifiers are not supported for {{{field_name}}}.",
                "template_format",
            )

    if not has_invalid_field and not any(field in _TIMESTAMP_FIELDS for field in fields):
        _issue(issues, path, "Template must include a timestamp placeholder.", "template_timestamp")

    if has_invalid_field:
        return

    sample_time = datetime(2026, 7, 23, 9, 0, 0)
    try:
        rendered = template.format(
            job=config.job.id,
            hostname="host",
            timestamp=sample_time,
            timestamp_utc=sample_time,
            source_hash="a" * 64,
            format=config.archive.format,
        )
    except (KeyError, ValueError) as error:
        _issue(issues, path, f"Template cannot be rendered: {error}", "template_render")
        return

    if not rendered:
        _issue(issues, path, "Template must produce a non-empty name.", "template_empty")
    if "/" in rendered:
        _issue(issues, path, "Template output must not contain '/'.", "template_separator")
    if ".." in rendered:
        _issue(issues, path, "Template output must not contain '..'.", "template_parent")
    if any(ord(character) < 32 or ord(character) == 127 for character in rendered):
        _issue(
            issues,
            path,
            "Template output must not contain null or control characters.",
            "template_control",
        )


def _validate_archive(config: JobConfig, issues: list[ValidationIssue]) -> None:
    archive_format = config.archive.format
    encryption = config.encryption
    if archive_format == "tar.zst":
        if encryption.mode != "none":
            _issue(
                issues,
                ("encryption", "mode"),
                "tar.zst requires encryption.mode: none.",
                "encryption_mode",
            )
        if encryption.password_file is not None:
            _issue(
                issues,
                ("encryption", "password_file"),
                "tar.zst forbids password_file.",
                "password_file_forbidden",
            )
    else:
        if encryption.mode != "password":
            _issue(
                issues,
                ("encryption", "mode"),
                "tar.7z requires encryption.mode: password.",
                "encryption_mode",
            )
        if encryption.password_file is None:
            _issue(
                issues,
                ("encryption", "password_file"),
                "tar.7z requires password_file.",
                "password_file_required",
            )
        if config.archive.compression_level > 9:
            _issue(
                issues,
                ("archive", "compression_level"),
                "tar.7z compression level must be from 1 through 9.",
                "compression_level",
            )


def _validate_retention(config: JobConfig, issues: list[ValidationIssue]) -> None:
    retention = config.retention
    if not any(
        (retention.hourly, retention.daily, retention.weekly, retention.monthly, retention.yearly)
    ):
        _issue(
            issues,
            ("retention",),
            "At least one retention tier must be greater than zero.",
            "retention_empty",
        )


def _validate_schedule(config: JobConfig, issues: list[ValidationIssue]) -> None:
    schedule = config.schedule
    if (schedule.at is None) == (schedule.window is None):
        _issue(
            issues,
            ("schedule",),
            "Exactly one of schedule.at and schedule.window must be set.",
            "schedule_time_choice",
        )

    times: list[tuple[tuple[str, ...], str]] = []
    if schedule.at is not None:
        times.append((("schedule", "at"), schedule.at))
    if schedule.window is not None:
        endpoints = schedule.window.split("-")
        if len(endpoints) != 2:
            _issue(
                issues,
                ("schedule", "window"),
                "Window must use HH:MM-HH:MM.",
                "schedule_window",
            )
        else:
            times.extend(
                (
                    (("schedule", "window"), endpoints[0]),
                    (("schedule", "window"), endpoints[1]),
                )
            )
            start = _minutes(endpoints[0])
            end = _minutes(endpoints[1])
            if start is not None and end is not None and end <= start:
                _issue(
                    issues,
                    ("schedule", "window"),
                    "Window end must be later than its start on the same day.",
                    "schedule_window_order",
                )

    for path, value in times:
        match = _TIME_PATTERN.fullmatch(value)
        if match is None:
            _issue(issues, path, "Time must use valid 24-hour HH:MM.", "schedule_time")
        elif schedule.interval == "hourly" and match.group("hour") != "00":
            _issue(
                issues,
                path,
                "Hourly schedule times must use hour 00.",
                "schedule_hourly_offset",
            )

    day = schedule.day
    if schedule.interval in {"hourly", "daily"}:
        if day is not None:
            _issue(
                issues,
                ("schedule", "day"),
                f"{schedule.interval} schedules forbid day.",
                "schedule_day",
            )
    elif schedule.interval == "weekly":
        if not isinstance(day, str) or day.lower() not in _WEEKDAYS:
            _issue(
                issues,
                ("schedule", "day"),
                "Weekly schedules require a weekday name.",
                "schedule_day",
            )
    elif not isinstance(day, int) or isinstance(day, bool):
        _issue(
            issues,
            ("schedule", "day"),
            "Monthly schedules require a day from 1 through 28.",
            "schedule_day",
        )


def _minutes(value: str) -> int | None:
    match = _TIME_PATTERN.fullmatch(value)
    if match is None:
        return None
    return int(match.group("hour")) * 60 + int(match.group("minute"))


def _validate_hooks(config: JobConfig, issues: list[ValidationIssue]) -> None:
    for phase in (
        "before_check",
        "before_archive",
        "after_archive",
        "on_success",
        "on_failure",
        "on_unchanged",
    ):
        hook = getattr(config.hooks, phase)
        if hook is None:
            continue
        if not PurePosixPath(hook.command[0]).is_absolute():
            _issue(
                issues,
                ("hooks", phase, "command", 0),
                "Hook executable path must be absolute.",
                "hook_path",
            )
        for index, element in enumerate(hook.command):
            if "\0" in element:
                _issue(
                    issues,
                    ("hooks", phase, "command", index),
                    "Hook command element contains a null character.",
                    "hook_null",
                )


def _validate_job_filename(
    config: JobConfig,
    config_path: Path | None,
    issues: list[ValidationIssue],
) -> None:
    if config_path is None:
        return
    expected = config_path.stem
    if config.job.id != expected:
        _issue(
            issues,
            ("job", "id"),
            f"Job ID must match configuration filename stem {expected!r}.",
            "job_filename",
        )
