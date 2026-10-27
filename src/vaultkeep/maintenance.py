"""Installer-only job rendering and restoration, executed as the owning account.

JSON travels over stdin/stdout. No shell commands or job hooks are executed, and
refreshing a unit never enables, starts, or reloads a service manager.
"""

from __future__ import annotations

import base64
import grp
import json
import os
import pwd
import re
import shlex
import stat
import sys
import tempfile
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any


def read_file(path: Path) -> dict[str, Any] | None:
    """Snapshot a regular file without following its final symlink."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"not a regular managed file: {path}")
        content = stream.read()
    return {
        "content": base64.b64encode(content).decode("ascii"),
        "mode": stat.S_IMODE(info.st_mode),
        "atime_ns": info.st_atime_ns,
        "mtime_ns": info.st_mtime_ns,
    }


def command_selection(content: str, *, monitor: bool, user: bool) -> tuple[Path, dict[str, str]]:
    """Decode only the literal argument forms written by Vaultkeep's renderers."""
    from vaultkeep.installation import installation_path

    commands = [line[10:] for line in content.splitlines() if line.startswith("ExecStart=")]
    commands = [command for command in commands if command]
    if len(commands) != 1 or "\\\n" in content:
        raise ValueError("managed service requires exactly one literal ExecStart")
    raw = commands[0]
    # Generated units escape literal systemd expansions as %% and $$. Refuse
    # custom specifiers, environment substitutions and C-style escapes.
    words = shlex.split(raw)
    if (
        any(re.search(r"(?<!%)%(?!%)", word) for word in words)
        or any(re.search(r"(?<!\$)\$(?!\$)", word) for word in words[1:])
        or re.search(r"(?<!\\)(?:\\\\)*\\x", raw)
    ):
        raise ValueError("custom systemd expansions require manual review")
    argv = [
        word.replace("%%", "%") if index == 0 else word.replace("%%", "%").replace("$$", "$")
        for index, word in enumerate(words)
    ]
    # Older releases generated the default executable even with a custom layout.
    executables = {"/usr/local/bin/vaultkeep", str(installation_path("VAULTKEEP_BIN_LINK"))}
    if not argv or argv.pop(0) not in executables:
        raise ValueError("unexpected managed service executable")
    if user and (not argv or argv.pop(0) != "--user"):
        raise ValueError("user service requires --user")
    # Before the unified CLI, per-job monitors placed 'trigger' before options.
    if monitor and argv and argv[0] == "trigger":
        argv.pop(0)
        suffix = ["monitor"]
    else:
        suffix = ["trigger", "monitor"] if monitor else ["run"]
    if len(argv) < 2 or argv.pop(0) != "--config":
        raise ValueError("managed service requires its original --config")
    config = Path(argv.pop(0))
    if not config.is_absolute():
        raise ValueError("managed service configuration must be absolute")
    params: dict[str, str] = {}
    while argv and argv[0] == "--param":
        if len(argv) < 2 or "=" not in argv[1]:
            raise ValueError("invalid managed service parameter")
        key, value = argv[1].split("=", 1)
        if key in params:
            raise ValueError("duplicate managed service parameter")
        params[key] = value
        del argv[:2]
    if argv != suffix:
        raise ValueError("custom managed service arguments require manual review")
    return config, params


def require_generated(
    original: str, rendered: str, *, variable: tuple[str, ...], path: Path | None = None
) -> None:
    """Permit regenerated values, but refuse extra or edited service directives."""

    def normalize(content: str) -> list[str]:
        return [
            line.split("=", 1)[0] + "=<generated>"
            if any(line.startswith(key + "=") for key in variable)
            else line
            for line in content.splitlines()
        ]

    if normalize(original) != normalize(rendered):
        resource = f": {path}" if path is not None else ""
        raise ValueError(f"locally modified managed unit requires review{resource}")


def managed_units(
    document: dict[str, Any], manager: Any
) -> tuple[set[str], set[Path], dict[str, str]]:
    """Identify managed files and selections without loading YAML or runtime state."""
    timers: set[str] = set()
    monitors: set[Path] = set()
    for root in map(Path, document["unit_roots"]):
        if document["user"]:
            timers.update(path.name[10:-6] for path in root.glob("vaultkeep@*.timer"))
            monitors.update(root.glob("vaultkeep-trigger@*.service"))
        else:
            timers.update(path.name[10:-8] for path in root.glob("vaultkeep@*.timer.d"))
    registry = manager._registry()
    timers.update(registry)
    user = document["user"]
    for instance in sorted(timers):
        timer = manager._timer_drop_in_path(instance)
        service = manager._service_drop_in_path(instance)
        if instance not in registry or registry[instance] != str(timer):
            raise ValueError(f"timer ownership registry requires review: {timer}")
        if read_file(timer) is None:
            raise ValueError(f"managed timer is missing: {timer}")
        if user or service.exists() or service.is_symlink():
            snapshot = read_file(service)
            if snapshot is None:
                raise ValueError(f"managed service is missing: {service}")
            try:
                command_selection(
                    base64.b64decode(snapshot["content"]).decode(), monitor=False, user=user
                )
            except ValueError as error:
                raise ValueError(f"{service}: {error}") from error
        for directory, allowed in (
            (manager.paths.units_root / f"vaultkeep@{instance}.timer.d", {timer}),
            (manager.paths.units_root / f"vaultkeep@{instance}.service.d", {service}),
        ):
            if any(path not in allowed for path in directory.glob("*.conf")):
                raise ValueError(f"local schedule overrides require review: {directory}")
    for path in sorted(monitors):
        snapshot = read_file(path)
        if snapshot is None:
            raise ValueError(f"managed monitor disappeared: {path}")
        content = base64.b64decode(snapshot["content"]).decode()
        if not content.startswith(f"# Managed by Vaultkeep: {path.name}\n"):
            raise ValueError(f"unmanaged monitor service: {path}")
        if path.with_name(path.name + ".d").exists():
            raise ValueError(f"local monitor overrides require review: {path}")
        try:
            command_selection(content, monitor=True, user=True)
        except ValueError as error:
            raise ValueError(f"{path}: {error}") from error
    return timers, monitors, registry


@contextmanager
def configuration_context(path: Path) -> Iterator[None]:
    """Retain the selected file and structured fields through installer layers."""
    from vaultkeep.errors import ApplicationIdentityError, ConfigurationError, VaultkeepError

    try:
        yield
    except ApplicationIdentityError as error:
        failure = ConfigurationError((error.issue,))
        failure.config_path = path.absolute()
        raise failure from error
    except VaultkeepError as error:
        error.config_path = path.absolute()
        raise


def configuration_details(error: Any) -> dict[str, Any]:
    """Return safe issue summaries; parser diagnostics can contain source text."""
    from vaultkeep.errors import ConfigurationError, ValidationIssue

    config_path = error.config_path
    assert config_path is not None
    messages = {
        "yaml_parse": "Invalid YAML syntax; correct the file and retry.",
        "yaml_read": "Cannot read configuration file; check permissions and encoding.",
        "yaml_missing": "Configuration file does not exist.",
    }
    issues = [
        {"path": issue.dotted_path, "message": messages.get(issue.code, issue.message)}
        for issue in (
            error.issues
            if isinstance(error, ConfigurationError)
            else (ValidationIssue((), str(error), "job_validation"),)
        )
    ]
    cause = error.__cause__
    if isinstance(error, ConfigurationError):
        from pydantic import ValidationError

        if isinstance(cause, ValidationError):
            details = {
                ValidationIssue(tuple(detail["loc"]), "", "").dotted_path: detail
                for detail in cause.errors(include_url=False, include_context=False)
            }
            for item in issues:
                detail = details.get(item["path"])
                if detail is None:
                    continue
                value = detail.get("input")
                # Commands, environment matches, password settings and parameter
                # selections may carry credentials. Never summarize their values.
                field = item["path"]
                sensitive = (
                    field
                    in (
                        "encryption",
                        "hooks",
                        "notifications",
                        "application.matcher",
                    )
                    or field.startswith(
                        (
                            "encryption.password_file",
                            "notifications.command",
                            "application.matcher.env_eq",
                            "application.matcher.argv_tokens",
                            "application.matcher.cmdline_regex",
                            "trigger.params",
                        )
                    )
                    or (field.startswith("hooks.") and not field.endswith(".timeout_seconds"))
                )
                try:
                    rendered = json.dumps(value, ensure_ascii=False, default=str)
                except (TypeError, ValueError):
                    # Recursive YAML values must not hide the validation error.
                    continue
                sensitive_text = re.search(
                    r"secret|password|passphrase|token|credential|://|\\[rn]", rendered, re.I
                )
                if (
                    "input" in detail
                    and detail.get("type") != "missing"
                    and len(rendered) <= 80
                    and not sensitive
                    and not sensitive_text
                ):
                    item["message"] = item["message"].rstrip(".") + f"; invalid value: {rendered}"
        elif any(issue.code == "yaml_parse" for issue in error.issues):
            mark = getattr(cause, "problem_mark", None)
            if mark is not None:
                issues[0]["message"] = (
                    f"Invalid YAML syntax at line {mark.line + 1}, column {mark.column + 1}; "
                    "correct the file and retry."
                )
            # The parser's context points to the container being parsed. Name
            # its block property when identifiable, without copying YAML values.
            context = getattr(cause, "context_mark", None)
            if context is not None:
                try:
                    lines = config_path.read_text().splitlines()
                    parents: list[tuple[int, str]] = []
                    for number, line in enumerate(lines[: context.line + 1]):
                        match = re.match(r"^( *)([A-Za-z_][\w-]*):(?:\s|$)", line)
                        if match:
                            if number == context.line and context.column < match.end():
                                continue
                            indent = len(match[1])
                            parents = [(level, key) for level, key in parents if level < indent]
                            parents.append((indent, match[2]))
                    if parents:
                        issues[0]["path"] = ".".join(key for _, key in parents)
                except (OSError, UnicodeError):
                    pass
    return {
        "job": config_path.stem,
        "file": str(config_path),
        "issues": issues,
    }


def validate_execution_identity(*, user: bool) -> None:
    """Check accounts/groups used by generated services, without inventing YAML fields."""
    from vaultkeep.errors import ConfigurationError, ValidationIssue

    try:
        account = pwd.getpwuid(os.geteuid()) if user else pwd.getpwnam("root")
    except KeyError as error:
        name = f"UID {os.geteuid()}" if user else "'root'"
        raise ConfigurationError(
            (
                ValidationIssue(
                    (),
                    f"User {name} does not exist (required by the backup service).",
                    "user_missing",
                ),
            )
        ) from error
    try:
        if user:
            grp.getgrgid(account.pw_gid)
        else:
            grp.getgrnam("root")
    except KeyError as error:
        name = f"GID {account.pw_gid}" if user else "'root'"
        raise ConfigurationError(
            (
                ValidationIssue(
                    (),
                    f"Group {name} does not exist (required by the backup service).",
                    "group_missing",
                ),
            )
        ) from error


def require_automation_enabled(enabled: bool, section: str) -> None:
    from vaultkeep.errors import ConfigurationError, ValidationIssue

    if not enabled:
        launcher = "installed schedule" if section == "schedule" else "active monitor"
        action = "remove the schedule" if section == "schedule" else "disable the monitor"
        raise ConfigurationError(
            (
                ValidationIssue(
                    (section, "enabled"),
                    f"Value false conflicts with the {launcher}; set true or {action} "
                    "before maintenance.",
                    "automation_disabled",
                ),
            )
        )


def check_configurations(document: dict[str, Any]) -> dict[str, Any]:
    """Check every managed YAML and installed selection with this release's schema."""
    from vaultkeep.config import load_config
    from vaultkeep.errors import VaultkeepError
    from vaultkeep.schedule import TimerManager, TimerPaths

    user = document["user"]
    paths = TimerPaths(**{key: Path(value) for key, value in document["paths"].items()})
    manager = TimerManager(paths, user_mode=user)
    timers, monitors, _ = managed_units(document, manager)
    try:
        configurations = {path for path in paths.jobs_root.iterdir() if path.suffix == ".yaml"}
    except FileNotFoundError:
        configurations = set()
    for instance in sorted(timers):
        service = manager._service_drop_in_path(instance)
        snapshot = read_file(service)
        if snapshot is None:
            configurations.add(paths.jobs_root / f"{instance}.yaml")
        else:
            config, _ = command_selection(
                base64.b64decode(snapshot["content"]).decode(), monitor=False, user=user
            )
            configurations.add(config)
    for path in sorted(monitors):
        snapshot = read_file(path)
        if snapshot is None:
            raise ValueError(f"managed monitor disappeared: {path}")
        config, _ = command_selection(
            base64.b64decode(snapshot["content"]).decode(), monitor=True, user=True
        )
        configurations.add(config)
    errors = []
    for path in sorted(configurations):
        try:
            with configuration_context(path):
                job_config = load_config(path)
                validate_execution_identity(user=user)
                if job_config.application is not None:
                    from vaultkeep.application import resolve_application

                    resolve_application(job_config.application, validate_directory=False)
        except VaultkeepError as error:
            traceback.print_exc(file=sys.stderr)
            errors.append(configuration_details(error))
    if not errors:
        # Rendering is read-only. Reuse the real refresh planner, including saved
        # parameter selections and checks on installed units, before any pause.
        try:
            plan(document)
        except VaultkeepError as error:
            if error.config_path is None:
                raise
            traceback.print_exc(file=sys.stderr)
            errors.append(configuration_details(error))
    return {"errors": errors}


def plan(document: dict[str, Any]) -> dict[str, Any]:
    """Render existing jobs; retain snapshots before any mutation."""
    from vaultkeep.backup.workflow import load_validated_config
    from vaultkeep.schedule import TimerManager, TimerPaths, render_schedule
    from vaultkeep.trigger.config import load_config as load_trigger
    from vaultkeep.trigger.service import render_service, service_name

    user = document["user"]
    paths = TimerPaths(**{key: Path(value) for key, value in document["paths"].items()})
    manager = TimerManager(paths, user_mode=user)
    changes = []
    jobs = []

    def change(path: Path, content: str | None, before: dict[str, Any] | None) -> None:
        if before is None and content is None:
            return
        after = base64.b64encode(content.encode()).decode("ascii") if content is not None else None
        changes.append({"path": str(path), "before": before, "after": after})

    timers, monitors, _ = managed_units(document, manager)
    for instance in sorted(timers):
        timer = manager._timer_drop_in_path(instance)
        service = manager._service_drop_in_path(instance)
        timer_snapshot = read_file(timer)
        if timer_snapshot is None:
            raise ValueError(f"managed timer disappeared: {timer}")
        service_content = None
        service_snapshot = None
        if user or service.exists() or service.is_symlink():
            service_snapshot = read_file(service)
            if service_snapshot is None:
                raise ValueError(f"managed service is missing: {service}")
            config_path, params = command_selection(
                base64.b64decode(service_snapshot["content"]).decode(), monitor=False, user=user
            )
            service_content = base64.b64decode(service_snapshot["content"]).decode()
        else:
            config_path, params = paths.jobs_root / f"{instance}.yaml", {}
        with configuration_context(config_path):
            config = load_validated_config(config_path, runtime_params=params)
            manager._load_managed(config_path, require_enabled=False, runtime_params=params)
            require_automation_enabled(config.schedule.enabled, "schedule")
            validate_execution_identity(user=user)
        if manager._instance_id(config_path, config.job.id, params) != instance:
            raise ValueError(f"job identity changed: {timer}")
        rendered_timer = manager._timer_content(instance, render_schedule(config))
        rendered_service = manager._service_content(config_path, instance, params)
        require_generated(
            base64.b64decode(timer_snapshot["content"]).decode(),
            rendered_timer,
            variable=("OnCalendar", "RandomizedDelaySec", "FixedRandomDelay", "Persistent"),
            path=timer,
        )
        if service_content is not None:
            if rendered_service is None:
                raise ValueError(f"unexpected service override requires review: {service}")
            require_generated(
                service_content, rendered_service, variable=("ExecStart",), path=service
            )
        change(timer, rendered_timer, timer_snapshot)
        change(service, rendered_service, service_snapshot)
        jobs.append(
            {
                "config": str(config_path),
                "params": params,
                "unit": f"vaultkeep@{instance}.timer",
                "monitor": False,
            }
        )
    for path in sorted(monitors):
        snapshot = read_file(path)
        if snapshot is None:
            raise ValueError(f"managed monitor disappeared: {path}")
        content = base64.b64decode(snapshot["content"]).decode()
        config_path, params = command_selection(content, monitor=True, user=True)
        with configuration_context(config_path):
            validate_execution_identity(user=user)
            load_validated_config(config_path, runtime_params=params)
            trigger = load_trigger(config_path, runtime_params=params)
            if path.name in document.get("active", []):
                require_automation_enabled(bool(trigger.enabled_instances()), "trigger")
        if service_name(trigger) != path.name:
            raise ValueError(f"job identity changed: {path}")
        rendered = render_service(trigger)
        require_generated(content, rendered, variable=("ExecStart",), path=path)
        change(path, rendered, snapshot)
        jobs.append(
            {"config": str(config_path), "params": params, "unit": path.name, "monitor": True}
        )
    return {"changes": changes, "jobs": jobs}


def apply(document: dict[str, Any], *, restore: bool = False) -> None:
    """Write as the account, refusing to clobber concurrent edits on apply/rollback."""
    errors = []
    for change in document["changes"]:
        try:
            path = Path(change["path"])
            current = read_file(path)
            content = current["content"] if current else None
            before = change["before"]
            original = before["content"] if before else None
            if content not in (original, change["after"]):
                raise ValueError(f"managed unit changed during maintenance: {path}")
            target = original if restore else change["after"]
            if content == target:
                continue
            if target is None:
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                # Keep restoration stdlib-only: it must also work if the
                # candidate interpreter/package is damaged or already gone.
                descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".vaultkeep-")
                try:
                    with os.fdopen(descriptor, "wb") as stream:
                        stream.write(base64.b64decode(target))
                        stream.flush()
                        os.fsync(stream.fileno())
                        os.fchmod(stream.fileno(), before["mode"] if before else 0o644)
                    os.replace(temporary, path)
                finally:
                    Path(temporary).unlink(missing_ok=True)
                if restore and before:
                    os.utime(path, ns=(before["atime_ns"], before["mtime_ns"]))
        except (OSError, ValueError) as error:
            if not restore:
                raise
            errors.append(str(error))
    if errors:
        raise ValueError("; ".join(errors))


def validate(document: dict[str, Any]) -> None:
    """Validate all jobs and runtime readiness only for launchers being resumed."""
    from vaultkeep.backup.workflow import load_validated_config, validate_job
    from vaultkeep.trigger.config import load_config as load_trigger

    for job in document["jobs"]:
        config_path = Path(job["config"])
        params = job["params"]
        with configuration_context(config_path):
            load_validated_config(config_path, runtime_params=params)
            if job["unit"] not in document["active"]:
                continue
            validate_job(config_path, runtime_params=params)
            if job["monitor"]:
                from vaultkeep.trigger.paths import state_root
                from vaultkeep.trigger.preflight import runtime_preflight
                from vaultkeep.trigger.systemd import RealSystemdController

                config = load_trigger(config_path, runtime_params=params)
                require_automation_enabled(bool(config.enabled_instances()), "trigger")
                runtime_preflight(
                    config, RealSystemdController(), state_root(config.scope), validate_backup=False
                )


def main() -> None:
    action = sys.argv[1]
    document = json.load(sys.stdin)
    if action == "restore":
        apply(document, restore=True)
        return
    from vaultkeep.errors import VaultkeepError

    try:
        if action == "check-launchers":
            from vaultkeep.schedule import TimerManager, TimerPaths

            paths = TimerPaths(**{key: Path(value) for key, value in document["paths"].items()})
            managed_units(document, TimerManager(paths, user_mode=document["user"]))
        elif action == "plan":
            print(json.dumps(plan(document)))
        elif action == "check-configurations":
            print(json.dumps(check_configurations(document)))
        elif action == "apply":
            apply(document)
        elif action == "validate":
            validate(document)
        else:
            raise ValueError(f"unknown maintenance action: {action}")
    except VaultkeepError as error:
        if error.config_path is None:
            raise
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({"errors": [configuration_details(error)]}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        traceback.print_exc(file=sys.stderr)
        if isinstance(error, OSError):
            reason = (
                "Required resource does not exist"
                if isinstance(error, FileNotFoundError)
                else "Permission denied"
                if isinstance(error, PermissionError)
                else f"Filesystem operation failed ({error.strerror or str(error)})"
            )
            message = f"{reason}: {error.filename or 'managed job files'}."
        elif isinstance(error, ValueError) and not isinstance(error, json.JSONDecodeError):
            message = str(error)
        else:
            message = "Internal managed-job failure; see diagnostics log for technical details."
        print(json.dumps({"failure": message}))
        raise SystemExit(1) from None
