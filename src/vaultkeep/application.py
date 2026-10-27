"""Shared owner-aware application resolution and offline enforcement."""

from __future__ import annotations

import os
import pwd
from pathlib import Path

from vaultkeep.config.models import ApplicationConfig, JobConfig
from vaultkeep.errors import ApplicationIdentityError, SourceDiscoveryError, ValidationIssue


def resolve_application(
    application: ApplicationConfig, *, validate_directory: bool = True
) -> tuple[ApplicationConfig, int]:
    """Resolve paths against the configured account, never the backup executor."""
    try:
        account = pwd.getpwnam(application.user)
    except KeyError as error:
        raise ApplicationIdentityError(
            ValidationIssue(
                ("application", "user"),
                f"User {application.user!r} does not exist.",
                "user_missing",
            )
        ) from error
    except ValueError as error:
        raise ApplicationIdentityError(
            ValidationIssue(
                ("application", "user"),
                f"Invalid user name {application.user!r}.",
                "user_invalid",
            )
        ) from error
    except OSError as error:
        raise ApplicationIdentityError(
            ValidationIssue(
                ("application", "user"),
                f"Cannot look up user {application.user!r}; check the system account service.",
                "user_lookup",
            )
        ) from error

    def resolve(value: str, field: tuple[str | int, ...]) -> str:
        expanded = account.pw_dir + value[1:] if value == "~" or value.startswith("~/") else value
        try:
            return str(Path(expanded).resolve(strict=False))
        except (OSError, ValueError) as error:
            raise ApplicationIdentityError(
                ValidationIssue(
                    field,
                    "Cannot resolve path; check permissions and symlinks.",
                    "application_path",
                )
            ) from error

    directory = Path(resolve(application.data_dir, ("application", "data_dir")))
    if validate_directory:
        try:
            if not directory.is_dir() or directory.stat().st_uid != account.pw_uid:
                raise ValueError("data directory must be owned by the configured application user")
        except (OSError, ValueError) as error:
            raise ApplicationIdentityError(
                ValidationIssue(
                    ("application", "data_dir"),
                    f"Data directory must exist and be owned by user {application.user!r}.",
                    "application_directory",
                )
            ) from error
    matcher = application.matcher.model_copy(
        update={
            "exe_realpath": resolve(
                application.matcher.exe_realpath, ("application", "matcher", "exe_realpath")
            )
            if application.matcher.exe_realpath
            else None,
            "entrypoints": [
                entry.model_copy(
                    update={
                        "value": resolve(
                            entry.value, ("application", "matcher", "entrypoints", index, "value")
                        )
                    }
                )
                if entry.kind != "java_main"
                else entry
                for index, entry in enumerate(application.matcher.entrypoints)
            ],
        }
    )
    resolved = application.model_copy(
        update={"user": account.pw_name, "data_dir": str(directory), "matcher": matcher}
    )
    resolved._owner_uid = account.pw_uid
    return resolved, account.pw_uid


def require_application_offline(config: JobConfig, *, owner_uid: int | None) -> None:
    """Require a reliable stopped observation; files mode does no process inspection."""
    if config.consistency.mode == "files":
        return
    from vaultkeep.trigger.config import matcher_from_canonical
    from vaultkeep.trigger.detection import detect_spec
    from vaultkeep.trigger.models import Observation
    from vaultkeep.trigger.process import PsutilProcessProvider

    app = config.application
    if app is None or owner_uid is None:
        raise SourceDiscoveryError("Offline validation requires a resolved application identity")
    if os.geteuid() not in (0, owner_uid):
        raise SourceDiscoveryError("Cannot inspect the configured application's process owner")
    try:
        directory = Path(app.data_dir)
        if (
            pwd.getpwnam(app.user).pw_uid != owner_uid
            or str(directory.resolve(strict=True)) != app.data_dir
            or not directory.is_dir()
            or directory.stat().st_uid != owner_uid
        ):
            raise ValueError("resolved application owner or data directory changed")
    except (KeyError, OSError, ValueError) as error:
        raise SourceDiscoveryError(
            f"Application identity is unavailable or changed: {error}"
        ) from error
    result = detect_spec(
        app=app.name,
        data_dir=app.data_dir,
        matcher=matcher_from_canonical(app.matcher.model_dump()),
        provider=PsutilProcessProvider(owner_uid=owner_uid),
    )
    if result.observation is not Observation.STOPPED:
        raise SourceDiscoveryError(
            f"Application must be offline: {result.observation.value}: {result.reason or app.name}"
        )
