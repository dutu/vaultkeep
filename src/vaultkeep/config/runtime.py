"""Explicit runtime template parameters for shared job configurations."""

from __future__ import annotations

import re
import string
from collections.abc import Mapping, Sequence

from vaultkeep.config.models import JobConfig
from vaultkeep.errors import ConfigurationError, IssuePathPart, ValidationIssue

PARAMETER_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PARAMETER_VALUE = re.compile(r"^[A-Za-z0-9_.:@+-]+$")
DESTINATION_NAME_FIELDS = frozenset(
    {"job", "hostname", "timestamp", "timestamp_utc", "source_hash", "format"}
)
RESERVED_PARAMETER_NAMES = DESTINATION_NAME_FIELDS | {"backup_id"}

_FORMATTER = string.Formatter()


def parse_runtime_parameters(arguments: Sequence[str]) -> dict[str, str]:
    """Parse repeatable KEY=VALUE CLI arguments into a validated mapping."""
    parameters: dict[str, str] = {}
    issues: list[ValidationIssue] = []
    for index, argument in enumerate(arguments):
        path: tuple[IssuePathPart, ...] = ("param", index)
        if "=" not in argument:
            issues.append(
                ValidationIssue(path, "Runtime parameter must use KEY=VALUE.", "param_syntax")
            )
            continue
        key, value = argument.split("=", maxsplit=1)
        if PARAMETER_NAME.fullmatch(key) is None:
            issues.append(
                ValidationIssue(
                    path,
                    "Runtime parameter name must match [A-Za-z_][A-Za-z0-9_]*.",
                    "param_name",
                )
            )
            continue
        if key in RESERVED_PARAMETER_NAMES:
            issues.append(
                ValidationIssue(
                    path,
                    f"Runtime parameter name {key!r} is reserved.",
                    "param_reserved",
                )
            )
            continue
        if key in parameters:
            issues.append(
                ValidationIssue(path, f"Duplicate runtime parameter: {key}.", "param_duplicate")
            )
            continue
        if not value:
            issues.append(
                ValidationIssue(path, "Runtime parameter value must not be empty.", "param_empty")
            )
            continue
        if PARAMETER_VALUE.fullmatch(value) is None:
            issues.append(
                ValidationIssue(
                    path,
                    "Runtime parameter value may contain only letters, digits, '.', '_', ':', "
                    "'@', '+', and '-'.",
                    "param_value",
                )
            )
            continue
        parameters[key] = value
    if issues:
        raise ConfigurationError(tuple(issues))
    return parameters


def resolve_runtime_config(
    config: JobConfig, parameters: Mapping[str, str] | None = None
) -> JobConfig:
    """Render runtime parameters into destination.root and validate required usage."""
    runtime_parameters = dict(parameters or {})
    issues: list[ValidationIssue] = []
    used = set(_runtime_fields_in_destination_root(config.destination.root, issues))
    used.update(_runtime_fields_in_destination_name(config.destination.name_template, issues))

    for key in sorted(set(runtime_parameters) - used):
        issues.append(
            ValidationIssue(
                ("param", key),
                f"Runtime parameter {key!r} is not used by this configuration.",
                "param_unused",
            )
        )

    if issues:
        raise ConfigurationError(tuple(issues))

    rendered_root = _render_destination_root(config.destination.root, runtime_parameters)
    if rendered_root == config.destination.root:
        return config
    return config.model_copy(
        update={
            "destination": config.destination.model_copy(update={"root": rendered_root}),
        }
    )


def _runtime_fields_in_destination_root(
    template: str, issues: list[ValidationIssue]
) -> frozenset[str]:
    path = ("destination", "root")
    try:
        parsed = tuple(_FORMATTER.parse(template))
    except ValueError as error:
        issues.append(ValidationIssue(path, f"Invalid template syntax: {error}", "template_syntax"))
        return frozenset()

    fields: set[str] = set()
    for _, field_name, format_spec, conversion in parsed:
        if field_name is None:
            continue
        if conversion is not None or format_spec:
            issues.append(
                ValidationIssue(
                    path,
                    f"Runtime parameter {{{field_name}}} in destination.root must not use "
                    "formatting.",
                    "template_format",
                )
            )
        if PARAMETER_NAME.fullmatch(field_name) is None:
            issues.append(
                ValidationIssue(
                    path,
                    f"Invalid runtime parameter placeholder: {{{field_name}}}.",
                    "template_placeholder",
                )
            )
            continue
        if field_name in RESERVED_PARAMETER_NAMES:
            issues.append(
                ValidationIssue(
                    path,
                    f"Built-in placeholder {{{field_name}}} is not supported in destination.root.",
                    "template_placeholder",
                )
            )
            continue
        fields.add(field_name)
    return frozenset(fields)


def _runtime_fields_in_destination_name(
    template: str, issues: list[ValidationIssue]
) -> frozenset[str]:
    path = ("destination", "name_template")
    try:
        parsed = tuple(_FORMATTER.parse(template))
    except ValueError:
        return frozenset()

    fields: set[str] = set()
    for _, field_name, format_spec, conversion in parsed:
        if field_name is None or field_name in DESTINATION_NAME_FIELDS or field_name == "backup_id":
            continue
        if PARAMETER_NAME.fullmatch(field_name) is None:
            issues.append(
                ValidationIssue(
                    path,
                    f"Invalid runtime parameter placeholder: {{{field_name}}}.",
                    "template_placeholder",
                )
            )
            continue
        if conversion is not None or format_spec:
            issues.append(
                ValidationIssue(
                    path,
                    f"Runtime parameter {{{field_name}}} in destination.name_template must not "
                    "use formatting.",
                    "template_format",
                )
            )
        fields.add(field_name)
    return frozenset(fields)


def _render_destination_root(template: str, parameters: Mapping[str, str]) -> str:
    try:
        return template.format(**parameters)
    except KeyError as error:
        missing = str(error).strip("'")
        raise ConfigurationError(
            (
                ValidationIssue(
                    ("destination", "root"),
                    f"Missing runtime parameter: {missing}.",
                    "param_missing",
                ),
            )
        ) from error
