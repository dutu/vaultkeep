"""Safe YAML 1.2 loading and strict schema validation."""

from __future__ import annotations

from difflib import get_close_matches
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from vaultkeep.config.models import JobConfig
from vaultkeep.errors import ConfigurationError, IssuePathPart, ValidationIssue

_FIELD_NAMES: dict[tuple[str, ...], tuple[str, ...]] = {
    (): (
        "config_version",
        "job",
        "sources",
        "exclude",
        "source_options",
        "destination",
        "archive",
        "encryption",
        "retention",
        "schedule",
        "hooks",
        "logging",
    ),
    ("job",): ("id",),
    ("sources",): ("path", "exclude"),
    ("source_options",): ("follow_symlinks", "cross_filesystems", "ignore_missing"),
    ("destination",): ("root", "name_template", "marker_file", "require_mount"),
    ("archive",): ("format", "compression_level"),
    ("encryption",): ("mode", "password_file"),
    ("retention",): ("hourly", "daily", "weekly", "monthly", "yearly"),
    ("schedule",): ("enabled", "interval", "window", "at", "day", "persistent"),
    ("hooks",): (
        "before_check",
        "before_archive",
        "after_archive",
        "on_success",
        "on_failure",
        "on_unchanged",
    ),
    ("hook",): ("command", "timeout_seconds"),
    ("logging",): ("level", "include_command_output"),
}
_HOOK_PHASES = frozenset(_FIELD_NAMES[("hooks",)])


def load_config(path: Path) -> JobConfig:
    """Load one file through safe YAML parsing and strict schema validation."""
    document = _parse_yaml(path)
    if not isinstance(document, dict):
        raise ConfigurationError(
            (
                ValidationIssue(
                    (),
                    "The YAML document root must be a mapping.",
                    "mapping_type",
                ),
            )
        )

    try:
        return JobConfig.model_validate(document)
    except ValidationError as error:
        raise ConfigurationError(_schema_issues(error)) from error


def _parse_yaml(path: Path) -> Any:
    yaml = YAML(typ="safe", pure=True)
    yaml.version = (1, 2)
    yaml.allow_duplicate_keys = False

    try:
        with path.open("r", encoding="utf-8") as stream:
            return yaml.load(stream)
    except (OSError, UnicodeError, YAMLError) as error:
        raise ConfigurationError(
            (
                ValidationIssue(
                    (),
                    f"Unable to parse YAML configuration: {error}",
                    "yaml_parse",
                ),
            )
        ) from error


def _schema_issues(error: ValidationError) -> tuple[ValidationIssue, ...]:
    issues: list[ValidationIssue] = []
    suggested_paths: set[str] = set()
    for detail in error.errors(include_url=False, include_context=False, include_input=False):
        location = tuple(_path_part(part) for part in detail["loc"])
        message = str(detail["msg"])
        code = str(detail["type"])
        if code == "extra_forbidden" and location and isinstance(location[-1], str):
            suggestion = _unknown_field_suggestion(location)
            message = "Unknown property."
            if suggestion is not None:
                message += f" Did you mean: {suggestion}?"
                suggested_paths.add(suggestion)
        issues.append(ValidationIssue(location, message, code))
    return tuple(
        issue
        for issue in issues
        if not (issue.code == "missing" and issue.dotted_path in suggested_paths)
    )


def _path_part(value: object) -> IssuePathPart:
    if isinstance(value, int):
        return value
    return str(value)


def _unknown_field_suggestion(location: tuple[IssuePathPart, ...]) -> str | None:
    unknown = location[-1]
    if not isinstance(unknown, str):
        return None

    parent_parts = tuple(part for part in location[:-1] if not isinstance(part, int))
    lookup = parent_parts
    if parent_parts and parent_parts[0] == "sources":
        lookup = ("sources",)
    elif parent_parts and parent_parts[-1] in _HOOK_PHASES:
        lookup = ("hook",)

    candidates = _FIELD_NAMES.get(lookup, ())
    matches = get_close_matches(unknown, candidates, n=1, cutoff=0.6)
    if not matches:
        return None

    display_parent = ".".join(str(part) for part in location[:-1] if not isinstance(part, int))
    return f"{display_parent}.{matches[0]}" if display_parent else matches[0]
